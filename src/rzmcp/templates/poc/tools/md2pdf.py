#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
md2pdf.py - render a guide written in Markdown as an A4 PDF.

  md2pdf.py INPUT.md OUTPUT.pdf

Only the Markdown the guides use is supported: ATX headings, paragraphs,
"-" and "1." lists (nested by indentation), fenced code blocks, pipe tables,
block quotes, horizontal rules, `code` and **bold**.

Layout rules:
  - code blocks, tables and block quotes are never split across pages
  - a heading is kept on the same page as the block that follows it, and so
    is a paragraph that introduces the next block (ends with ':')
  - code blocks never wrap: each block's font size is chosen so that its
    longest line fits the available width
  - inline code (file names, paths, variables) is never broken across lines,
    except in table cells, where long names may break after '-' or '/'

The HTML is printed with headless Firefox over Marionette, and Ghostscript
adds the "Page N" footer and the document title.
"""

import base64
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# --- Page geometry (points) --------------------------------------------------
MM = 72 / 25.4
PAGE_W, PAGE_H = 210 * MM, 297 * MM
MARGIN_TB, MARGIN_LR = 20 * MM, 20 * MM
TEXT_W = PAGE_W - 2 * MARGIN_LR

MONO_ADVANCE = 0.602            # DejaVu Sans Mono advance width, in em
CODE_PT_MAX, CODE_PT_MIN = 8.2, 6.0
CODE_PAD = 2 * 9 + 2            # left + right padding, plus borders
LIST_INDENT = 20                # matches the CSS below
QUOTE_INDENT = 2 * 11 + 3

CSS = """
@page { size: A4; }
html { font-family: "DejaVu Serif", serif; font-size: 9.6pt; color: #1f2933;
       line-height: 1.55; }
body { margin: 0; }

h1 { font-size: 21pt; line-height: 1.3; color: #1b3358; text-align: center;
     margin: 6pt 0 18pt; }
h2 { font-size: 15pt; color: #1b3358; margin: 6pt 0 8pt; }
h3 { font-size: 12.2pt; color: #2b5d8f; margin: 13pt 0 5pt; }
h4 { font-size: 10.6pt; color: #2b5d8f; margin: 11pt 0 4pt; }
h1, h2, h3, h4 { break-after: avoid; }

p { margin: 0 0 6pt; break-inside: avoid; }
p.body { text-indent: 1.6em; }
p.small { font-size: 8.4pt; color: #52606d; }
p.lead { margin: 9pt 0 3pt; color: #102a43; }
strong { color: #102a43; }

code { font-family: "DejaVu Sans Mono", monospace; font-size: 0.86em;
       white-space: nowrap; color: #102a43; }

hr { border: 0; border-top: 1px solid #c9d6e6; margin: 16pt 0 12pt; }

ul, ol { margin: 2pt 0 7pt; padding-left: 20pt; }
li { margin: 0 0 3pt; break-inside: avoid; }
li > p { margin: 0 0 4pt; }
ul > li::marker { content: "\\2013  "; color: #2b5d8f; font-weight: bold; }
ol > li::marker { color: #1b3358; font-weight: bold; }

.keep { break-inside: avoid; }

.code { break-inside: avoid; margin: 5pt 0 9pt; border: 1px solid #8fa7c4;
        border-radius: 2pt; }
.code .label { background: #1b3358; color: #ffffff; font-family: "DejaVu Sans Mono";
               font-size: 6.8pt; font-weight: bold; letter-spacing: 0.6pt;
               padding: 2.5pt 9pt; }
.code pre { margin: 0; background: #eef3fa; padding: 6pt 9pt;
            font-family: "DejaVu Sans Mono", monospace; line-height: 1.42;
            white-space: pre; overflow: hidden; color: #102a43; }

table { break-inside: avoid; border-collapse: collapse; width: 100%;
        margin: 5pt 0 10pt; font-size: 8.6pt; line-height: 1.4; }
th { background: #e6edf6; color: #102a43; font-weight: bold; text-align: left;
     border: 1px solid #b9c9dc; padding: 4pt 6pt; }
td { border: 1px solid #c9d6e6; padding: 4pt 6pt; vertical-align: top; }
td code, th code { white-space: normal; }
tr { break-inside: avoid; }

blockquote { break-inside: avoid; margin: 6pt 0 10pt; padding: 6pt 11pt;
             background: #f4f7fb; border-left: 3pt solid #5b84b1;
             font-size: 9pt; color: #334e68; }
blockquote p { margin: 0 0 3pt; }
blockquote p:last-child { margin: 0; }
"""


# --- Inline markup -------------------------------------------------------------
def inline(text, cell=False):
    codes = []

    def stash(m):
        code = html.escape(m.group(1), quote=False)
        if cell:
            code = re.sub(r'([-/])', r'\1<wbr>', code)
        codes.append('<code>%s</code>' % code)
        return '\x00%d\x00' % (len(codes) - 1)

    text = re.sub(r'`([^`]*)`', stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    return re.sub(r'\x00(\d+)\x00', lambda m: codes[int(m.group(1))], text)


# --- Block parser ----------------------------------------------------------------
LIST_RE = re.compile(r'^( *)(-|\d+\.) +(.*)$')


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def dedent(lines, n):
    return [l[n:] if l[:n].strip() == '' else l.lstrip(' ') for l in lines]


class Block:
    def __init__(self, html_text, kind, text=''):
        self.html, self.kind, self.text = html_text, kind, text


def code_block(lines, lang, width):
    longest = max((len(l) for l in lines), default=1)
    pt = min(CODE_PT_MAX, (width - CODE_PAD) / (MONO_ADVANCE * max(longest, 1)))
    if pt < CODE_PT_MIN:
        sys.stderr.write('md2pdf: warning: code line of %d characters is set at '
                         '%.1f pt\n' % (longest, pt))
    label = {'sh': 'SH', 'text': 'TEXT', 'plaintext': 'PLAINTEXT'}.get(lang, (lang or 'text').upper())
    body = html.escape('\n'.join(lines), quote=False)
    return ('<div class="code"><div class="label">%s</div>'
            '<pre style="font-size:%.2fpt">%s</pre></div>' % (label, pt, body))


def table_block(lines):
    rows = []
    for l in lines:
        cells = [c.strip() for c in l.strip().strip('|').split('|')]
        rows.append(cells)
    head, body = rows[0], [r for r in rows[2:]]
    h = ['<table><thead><tr>']
    h += ['<th>%s</th>' % inline(c, True) for c in head]
    h.append('</tr></thead><tbody>')
    for r in body:
        h.append('<tr>' + ''.join('<td>%s</td>' % inline(c, True) for c in r) + '</tr>')
    h.append('</tbody></table>')
    return ''.join(h)


def parse(lines, width, top=False):
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue

        if s.startswith('```'):
            lang = s[3:].strip()
            ind = indent_of(line)
            j = i + 1
            body = []
            while j < n and not lines[j].strip().startswith('```'):
                body.append(lines[j][ind:] if lines[j][:ind].strip() == '' else lines[j])
                j += 1
            blocks.append(Block(code_block(body, lang, width), 'code'))
            i = j + 1
            continue

        m = re.match(r'^(#{1,6}) +(.*)$', s)
        if m:
            lvl = len(m.group(1))
            blocks.append(Block('<h%d>%s</h%d>' % (lvl, inline(m.group(2)), lvl), 'heading'))
            i += 1
            continue

        if re.match(r'^-{3,}$', s):
            blocks.append(Block('<hr>', 'hr'))
            i += 1
            continue

        if s.startswith('|'):
            j = i
            while j < n and lines[j].strip().startswith('|'):
                j += 1
            blocks.append(Block(table_block(lines[i:j]), 'table'))
            i = j
            continue

        if s.startswith('>'):
            j = i
            inner = []
            while j < n and lines[j].strip().startswith('>'):
                inner.append(re.sub(r'^ *> ?', '', lines[j]))
                j += 1
            sub = parse(inner, width - QUOTE_INDENT)
            blocks.append(Block('<blockquote>%s</blockquote>' % join(sub), 'quote'))
            i = j
            continue

        m = LIST_RE.match(line)
        if m:
            ordered = m.group(2) != '-'
            base = indent_of(line)
            items = []
            j = i
            while j < n:
                m = LIST_RE.match(lines[j])
                if not m or indent_of(lines[j]) != base or (m.group(2) != '-') != ordered:
                    break
                content_ind = len(m.group(1)) + len(m.group(2)) + 1
                item = [' ' * content_ind + m.group(3)]
                k = j + 1
                while k < n:
                    l = lines[k]
                    if not l.strip():
                        # A blank line continues the item only if indented
                        # content follows.
                        nxt = next((x for x in lines[k + 1:] if x.strip()), None)
                        if nxt is not None and indent_of(nxt) >= content_ind:
                            item.append(l)
                            k += 1
                            continue
                        break
                    if indent_of(l) >= content_ind:
                        item.append(l)
                        k += 1
                        continue
                    if LIST_RE.match(l):
                        break
                    if indent_of(l) > base:     # lazy continuation
                        item.append(' ' * content_ind + l.strip())
                        k += 1
                        continue
                    break
                items.append((m.group(2), dedent(item, content_ind)))
                j = k
                # skip blank lines between items of the same list
                while j < n and not lines[j].strip():
                    nxt = next((x for x in lines[j:] if x.strip()), None)
                    mm = LIST_RE.match(nxt) if nxt else None
                    if mm and indent_of(nxt) == base:
                        j += 1
                    else:
                        break
            h = []
            if ordered:
                h.append('<ol start="%s">' % items[0][0].rstrip('.'))
            else:
                h.append('<ul>')
            for _, content in items:
                sub = parse(content, width - LIST_INDENT)
                if len(sub) == 1 and sub[0].kind == 'para':
                    h.append('<li>%s</li>' % sub[0].text)
                else:
                    h.append('<li>%s</li>' % join(sub))
            h.append('</ol>' if ordered else '</ul>')
            blocks.append(Block(''.join(h), 'list'))
            i = j
            continue

        # paragraph
        j = i
        para = []
        while j < n and lines[j].strip() and not (
                lines[j].strip().startswith(('```', '|', '>', '#'))
                or LIST_RE.match(lines[j]) or re.match(r'^-{3,}$', lines[j].strip())):
            para.append(lines[j].strip())
            j += 1
        text = inline(' '.join(para))
        raw = ' '.join(para)
        lead = re.fullmatch(r'\*\*[^*]+\*\*', raw) is not None
        cls = ''
        if lead:
            cls = ' class="lead"'
        elif top:
            cls = ' class="small"' if raw.startswith('(*)') else ' class="body"'
        b = Block('<p%s>%s</p>' % (cls, text), 'para', text)
        b.intro = lead or para[-1].endswith(':')
        blocks.append(b)
        i = j
    return blocks


def join(blocks):
    """Glue each heading, and each paragraph that introduces the next block,
    to that block, so that a page break cannot separate them."""
    out = []
    i = 0
    while i < len(blocks):
        group = [blocks[i]]
        while (group[-1].kind == 'heading' or getattr(group[-1], 'intro', False)) \
                and i + 1 < len(blocks) and blocks[i + 1].kind != 'hr':
            i += 1
            group.append(blocks[i])
        if len(group) > 1:
            out.append('<div class="keep">%s</div>' % ''.join(b.html for b in group))
        else:
            out.append(group[0].html)
        i += 1
    return '\n'.join(out)


def to_html(md_text, title):
    blocks = parse(md_text.splitlines(), TEXT_W, top=True)
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>%s</title>'
            '<style>%s</style></head><body>%s</body></html>'
            % (html.escape(title), CSS, join(blocks)))


# --- Printing ----------------------------------------------------------------------
def firefox_print(html_path, pdf_path, workdir):
    port = 2828 + (os.getpid() % 1000) + 1
    prof = os.path.join(workdir, 'profile')
    os.makedirs(prof)
    with open(os.path.join(prof, 'user.js'), 'w') as f:
        f.write('user_pref("marionette.port", %d);\n' % port)
    ff = subprocess.Popen(['firefox', '--headless', '--marionette', '--no-remote',
                           '--profile', prof],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                sock = socket.create_connection(('127.0.0.1', port))
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit('md2pdf: Firefox Marionette did not start')
        rf = sock.makefile('rb')

        def recv():
            n = b''
            while True:
                c = rf.read(1)
                if c == b':':
                    break
                if not c:
                    raise SystemExit('md2pdf: Marionette connection closed')
                n += c
            return json.loads(rf.read(int(n)))

        msg_id = [0]

        def cmd(name, params):
            msg_id[0] += 1
            data = json.dumps([0, msg_id[0], name, params]).encode()
            sock.sendall(str(len(data)).encode() + b':' + data)
            r = recv()
            if r[2]:
                raise SystemExit('md2pdf: %s: %s' % (name, r[2]))
            return r[3]

        recv()                                  # hello
        cmd('WebDriver:NewSession', {'capabilities': {}})
        cmd('WebDriver:Navigate', {'url': 'file://' + html_path})
        cm = 2.54 / 72
        r = cmd('WebDriver:Print', {
            'background': True, 'shrinkToFit': False, 'orientation': 'portrait',
            'page': {'width': 21.0, 'height': 29.7},
            'margin': {'top': MARGIN_TB * cm, 'bottom': (MARGIN_TB + 2 * MM) * cm,
                       'left': MARGIN_LR * cm, 'right': MARGIN_LR * cm}})
        with open(pdf_path, 'wb') as f:
            f.write(base64.b64decode(r['value']))
        try:
            cmd('Marionette:Quit', {})
        except SystemExit:
            pass
    finally:
        try:
            ff.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ff.kill()


def ps_string(s):
    return '(' + s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') + ')'


def add_footer(src, dst, title):
    x_right = PAGE_W - MARGIN_LR
    y = 12 * MM
    ps = (
        'userdict /md2pdf_pn 0 put '
        '<< /EndPage { exch pop 2 eq { false } { '
        '  userdict /md2pdf_pn userdict /md2pdf_pn get 1 add put '
        '  gsave /Helvetica findfont 7.5 scalefont setfont 0.47 0.53 0.6 setrgbcolor '
        '  userdict /md2pdf_pn get 10 string cvs /md2pdf_s exch def '
        '  %.2f (Page ) stringwidth pop md2pdf_s stringwidth pop add sub %.2f moveto '
        '  (Page ) show md2pdf_s show grestore true } ifelse } >> setpagedevice '
        '[ /Title %s /DOCINFO pdfmark'
        % (x_right, y, ps_string(title)))
    subprocess.run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-sDEVICE=pdfwrite',
                    '-dCompatibilityLevel=1.7', '-sOutputFile=' + dst,
                    '-c', ps, '-f', src], check=True)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], os.path.abspath(sys.argv[2])
    md_text = open(src, encoding='utf-8').read()
    m = re.search(r'^# +(.*)$', md_text, re.M)
    title = m.group(1).strip() if m else os.path.basename(src)

    # Snap Firefox can only read files under ~/snap/firefox.
    base = os.path.expanduser('~/snap/firefox/common')
    os.makedirs(base, exist_ok=True)
    work = tempfile.mkdtemp(prefix='md2pdf-', dir=base)
    try:
        html_path = os.path.join(work, 'doc.html')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(to_html(md_text, title))
        raw = os.path.join(work, 'raw.pdf')
        firefox_print(html_path, raw, work)
        add_footer(raw, dst, title)
        if os.environ.get('MD2PDF_KEEP_HTML'):
            shutil.copy(html_path, os.path.splitext(dst)[0] + '.html')
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
