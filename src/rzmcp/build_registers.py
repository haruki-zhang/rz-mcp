"""Layer B: structured registers for selected HW UM modules, with rule-based validation.

    PYTHONPATH=src python3 -m rzmcp.build_registers [--db data/rz.db]

Modules to parse are listed per document in the manifest (`register_modules = ["5.4", ...]`).
Requires layer A (section table) to be built first. Rebuilds only the layer-B tables.

Sources inside one module chapter of a Renesas HW UM:
  * base address table      -> module_base   (channel x address space)
  * register list table     -> register      (one row per register instance)
  * register description(s) -> register_def + field (bit table), one per register template
Every derived row keeps its page number. Validation findings go to register_issue and are
surfaced by the query layer next to the data they concern.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field as dfield
from pathlib import Path

from . import DB_PATH, ROOT, catalog
from .pdf import norm, raw_page_texts, strip_running_lines

SCHEMA = """
DROP TABLE IF EXISTS register_fts;
DROP TABLE IF EXISTS register_issue;
DROP TABLE IF EXISTS field;
DROP TABLE IF EXISTS register;
DROP TABLE IF EXISTS register_def;
DROP TABLE IF EXISTS module_base;
DROP TABLE IF EXISTS module;

CREATE TABLE module (
    module_id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL,
    name TEXT NOT NULL,                -- "WDT"
    title TEXT NOT NULL, section_number TEXT NOT NULL, page_start INTEGER, page_end INTEGER
);
CREATE TABLE module_base (
    module_id INTEGER NOT NULL, channel TEXT,           -- NULL = module has a single instance
    address_space TEXT NOT NULL,                        -- CA55 | CM33_S | CM33_NS
    base_address INTEGER NOT NULL, base_text TEXT NOT NULL, page INTEGER
);
CREATE TABLE register_def (            -- one register description section (may be a template, e.g. WDTCNT_n)
    def_id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL,
    template TEXT NOT NULL, title TEXT NOT NULL, params TEXT,
    section_number TEXT, page_start INTEGER, page_end INTEGER, summary TEXT,
    notes TEXT                         -- "Note n." / CAUTION / Remark blocks of the description section
);
CREATE TABLE register (                -- one concrete register from the register list table
    reg_id INTEGER PRIMARY KEY, module_id INTEGER NOT NULL, def_id INTEGER,
    name TEXT NOT NULL, full_name TEXT, channel TEXT,   -- channel NULL = same offset in every channel
    offset INTEGER NOT NULL, offset_text TEXT NOT NULL, size INTEGER NOT NULL,
    access TEXT, reset_text TEXT, reset_value INTEGER, page INTEGER
);
CREATE INDEX register_name ON register (name);
CREATE TABLE field (
    field_id INTEGER PRIMARY KEY, def_id INTEGER NOT NULL, ord INTEGER NOT NULL,
    name TEXT,                         -- NULL = reserved
    msb INTEGER NOT NULL, lsb INTEGER NOT NULL, access TEXT, reset_text TEXT, description TEXT
);
CREATE TABLE register_issue (module_id INTEGER, def_id INTEGER, reg_id INTEGER, rule TEXT NOT NULL, message TEXT NOT NULL);
CREATE VIRTUAL TABLE register_fts USING fts5(name, full_name, fields, body, tokenize='porter unicode61');
"""

HEX = r"[0-9A-Fa-f][0-9A-Fa-f_]*"
ADDR_RE = re.compile(rf"^(?:H[’']\s?({HEX})|({HEX})h)$")
INIT_RE = re.compile(rf"^(?:H[’']([0-9A-Fa-fxX_]+)|([0-9A-Fa-fxX][0-9A-Fa-fxX_]*)h|Undefined|undefined|-)$")
RW_RE = re.compile(r"^(?:R/W|R|W|RW|R0W1|R/W1C|W1C|R/WC|R/\(W\)|R\(W\))[*\d]*$")
IDENT_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
ADDR_TOKEN = re.compile(r"H[’']\s?[0-9A-Fa-f][0-9A-Fa-f_]*|\b(?=[0-9A-F_]*\d)[0-9A-F][0-9A-F_]*h\b")
TITLE_ABBR_RE = re.compile(r"\(\s*([A-Z<][A-Za-z0-9_<>]*)\s*(?=[()])")
PARAM_RE = re.compile(r"\(\s*([a-z])\s*=\s*([^()]*)\)")
BITSPEC_RE = re.compile(r"^\s*(b?\d+(?:\s*(?:to|-|:|,)\s*b?\d+)*)(?=\s{2,}|\s*$)")


def parse_hex(tok: str) -> int | None:
    m = ADDR_RE.match(tok.strip())
    if not m:
        return None
    return int((m.group(1) or m.group(2)).replace("_", ""), 16)


def parse_init(tok: str) -> int | None:
    m = INIT_RE.match(tok)
    if not m or not (m.group(1) or m.group(2)):
        return None
    s = (m.group(1) or m.group(2)).replace("_", "")
    return None if re.search(r"[xX]", s) else int(s, 16)


def canon_space(s: str) -> str:
    s = s.lower()
    if "a55" in s:
        return "CA55"
    if "m33" in s:
        return "CM33_NS" if "non" in s else "CM33_S"
    return s.strip() or "CA55"


def expand_values(spec: str) -> list[str]:
    """'0, 1, 2' | '1, 4, 6 or 7' | '0 to 7' -> list of strings."""
    out = []
    for part in re.split(r",|\bor\b|\band\b", spec):
        part = part.strip()
        m = re.match(r"^(\d+)\s*to\s*(\d+)$", part)
        if m:
            out += [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
        elif part:
            out.append(part)
    return out


# ---------------------------------------------------------------- base addresses
def parse_base_table(lines: list[tuple[int, str]]) -> list[dict]:
    """Find the base-address table in the module's register-overview pages and parse it.

    Handles three layouts seen in the RZ/G3L HW UM:
      vertical   : "<space> | <channel> | <desc> | H'addr"  (one address per row; WDT)
      horizontal : header "Channel 0  Channel 1 ...", rows "<space> addr addr ..."  (SCIFA)
      inline     : "<X_base>  0_1101_0000h (5101_0000h*1, 4101_0000h*2)" + notes  (CPG)
    """
    out: list[dict] = []
    start = next((i for i, (_, l) in enumerate(lines) if re.search(r"Base Address", l, re.I)), None)
    if start is None:
        return out
    block = []
    for pg, l in lines[start:]:
        if block and re.match(r"^\s*(Table\s+\d|\d+\.\d+\.\d+)", l) and "Base" not in l:
            break
        block.append((pg, l))
        if len(block) > 60:
            break

    notes = {m.group(1): m.group(2) for _, l in block
             if (m := re.match(r"^\s*Note\s*(\d+)\.?\s+(.*)$", l))}
    # inline form
    for pg, l in block:
        m = re.match(r"^\s*(<\w+>)\s+(\S+h)\s*\((.*)\)", l)
        if m:
            out.append({"channel": None, "space": "CA55", "addr": parse_hex(m.group(2)), "text": m.group(2), "page": pg})
            for a, n in re.findall(rf"({HEX}h)\s*\*\s*(\d+)", m.group(3)):
                out.append({"channel": None, "space": canon_space(notes.get(n, "")), "addr": parse_hex(a),
                            "text": a, "page": pg})
    if out:
        return out
    # horizontal form
    header = next(((i, l) for i, (_, l) in enumerate(block) if len(re.findall(r"Channel\s*\d+|\bCH\s*\d+", l)) >= 2), None)
    if header:
        chans = [re.sub(r"\s+", " ", c) for c in re.findall(r"Channel\s*\d+|\bCH\s*\d+", header[1])]
        space = None
        for pg, l in block[header[0] + 1:]:
            addrs = ADDR_TOKEN.findall(l)
            lead = ADDR_TOKEN.split(l)[0].strip()
            if len(addrs) >= 2:
                space = lead
                for ch, a in zip(chans, addrs):
                    out.append({"channel": ch, "space": space, "addr": parse_hex(a.replace(" ", "")), "text": a, "page": pg})
            elif space and lead and not addrs and len(lead) < 30:
                for o in out:
                    if o["space"] == space:
                        o["space"] = space + " " + lead
                space = space + " " + lead
            elif not l.strip() and out:
                continue
        for o in out:
            o["space"] = canon_space(o["space"])
        return out
    # vertical form
    space = ""
    for pg, l in block:
        addrs = ADDR_TOKEN.findall(l)
        cols = [c for c in re.split(r"\s{2,}", l.strip()) if c]
        if len(addrs) == 1 and cols:
            if re.match(r"(Cortex|CA55|CM33)", cols[0]):
                space = cols[0]
            ch = next((c for c in cols if re.search(r"\bCH\s*\d+|Channel\s*\d+", c)), None)
            out.append({"channel": ch, "space": space, "addr": parse_hex(addrs[0].replace(" ", "")),
                        "text": addrs[0], "page": pg})
        elif not addrs and cols and out and re.match(r"^Space", cols[0]):
            ext = cols[0]
            for o in out:
                if o["space"] == space:
                    o["space"] = space + " " + ext
            space = space + " " + ext
    for o in out:
        o["space"] = canon_space(o["space"])
    return out


# ---------------------------------------------------------------- register list
def parse_register_list(lines: list[tuple[int, str]], channels: list[str]) -> list[dict]:
    regs, has_rw, last_row_line = [], False, -10
    for i, (pg, l) in enumerate(lines):
        if "Abbreviation" in l and re.search(r"Offset|Address", l):
            has_rw = "R/W" in l
            continue
        tp = [(mm.start(), mm.group()) for mm in re.finditer(r"\S+", l)]
        t = [x for _, x in tp]
        if len(t) >= 5 and t[-1] in ("8", "16", "32", "64") and parse_hex(t[-2]) is not None and INIT_RE.match(t[-3]):
            k = len(t) - 3
            rw = None
            if has_rw and k - 1 >= 0 and RW_RE.match(t[k - 1]):
                rw = t[k - 1]
                k -= 1
            abbr = t[k - 1] if k - 1 >= 0 else ""
            if not IDENT_RE.match(abbr):
                continue
            abbr_col = tp[k - 1][0]
            left = " ".join(t[:k - 1])
            ch = None
            for c in sorted(channels, key=len, reverse=True):
                if left.startswith(c + " "):
                    ch, left = c, left[len(c) + 1:]
                    break
            if ch is None and channels and regs and regs[-1]["channel_explicit"]:
                ch = regs[-1]["channel"]           # channel cell spans rows
            regs.append({"name": abbr, "full_name": left, "channel": ch, "channel_explicit": ch is not None,
                         "offset": parse_hex(t[-2]), "offset_text": t[-2], "size": int(t[-1]), "access": rw,
                         "reset_text": t[-3], "reset_value": parse_init(t[-3]), "page": pg, "abbr_col": abbr_col})
            last_row_line = i
        elif regs and i == last_row_line + 1 and l.strip() and len(l.strip()) <= 24 \
                and not re.match(r"^\s*(to|-|Reserve)", l.strip()) and parse_hex(l.strip()) is None:
            # wrapped cell: decide by column whether it continues the name or the abbreviation
            for s, tok in ((mm.start(), mm.group()) for mm in re.finditer(r"\S+", l)):
                if s >= regs[-1]["abbr_col"] - 1 and re.fullmatch(r"[A-Z0-9_]+", tok):
                    regs[-1]["name"] += tok                  # "CPG_BUS_PERI_VIDEO1_MSTO" + "P"
                else:
                    regs[-1]["full_name"] += " " + tok       # "...Setting Register" + "1"
            last_row_line = i
    return regs


# ---------------------------------------------------------------- bit field table
def parse_bitspec(spec: str) -> tuple[int, int]:
    nums = [int(x) for x in re.findall(r"\d+", spec)]
    return max(nums), min(nums)


def _col_starts(header: str) -> dict[str, int] | None:
    pos = {}
    for key, pat in (("bit", r"\bBit\b(?!\s+Name)"), ("name", r"Bit\s+Name"), ("init", r"Initial\s+Value|\bValue\b"),
                     ("rw", r"R/W"), ("desc", r"Description")):
        m = re.search(pat, header)
        if not m:
            return None
        pos[key] = m.start()
    return pos


FOOTNOTE_RE = re.compile(r"\*\s*\d*(?:,\s*\*?\d+)*$")


def _split_cells(toks: list[tuple[int, str]], cols: dict[str, int]) -> tuple[list, list, list, list]:
    name, init, rw, desc = [], [], [], []
    for s, tok in toks:
        if s < cols["init"] - 1:
            name.append(tok)
        elif s < cols["rw"] - 1:
            init.append(tok)
        elif s < cols["desc"] - 2 and (RW_RE.match(tok) or re.fullmatch(r"\*\d+", tok)) and not desc:
            rw.append(tok)                     # only real access tokens: "R/(W) Receive..." -> desc gets "Receive"
        else:
            desc.append(tok)
    return name, init, rw, desc


def parse_field_table(lines: list[str]) -> list[dict]:
    fields, cols, cur, dangling = [], None, None, False
    for l in lines:
        if "Bit Name" in l and "R/W" in l:
            cols = _col_starts(l)
            continue
        if cols is None or not l.strip() or l.strip() == "Initial":   # first line of a repeated 2-line header
            continue
        if dangling and cur is not None:      # "b15 to" on one line, "b11" on the next
            mm = re.match(r"^\s*(b?\d+)\b", l)
            dangling = False
            if mm and mm.start(1) < cols["name"] - 1:
                cur["bits"] += " " + mm.group(1)
                cur["msb"], cur["lsb"] = parse_bitspec(cur["bits"])
                l = " " * mm.end() + l[mm.end():]
        m = BITSPEC_RE.match(l)
        md = re.match(r"^\s*(b?\d+\s+to)(?=\s{2,}|\s*$)", l)
        if md and not m:
            m, dangling = md, True
        if m and m.start(1) < cols["name"] - 1:
            toks = [(mm.start(), mm.group()) for mm in re.finditer(r"\S+", l) if mm.start() >= m.end(1)]
            name, init, rw, desc = _split_cells(toks, cols)
            # footnote marker split off a name: "TEIE*" "1" -> "TEIE*1"
            if len(name) >= 2 and name[-2].endswith("*") and name[-1].isdigit():
                name[-2:] = [name[-2] + name[-1]]
            # a name that overflows into the init column ("SSC_MODE_W 0h")
            if not init and len(name) > 1:
                init = [name.pop()]
            nm = FOOTNOTE_RE.sub("", " ".join(name)).strip()
            msb, lsb = parse_bitspec(m.group(1)) if not dangling else (int(re.findall(r"\d+", m.group(1))[0]),) * 2
            cur = {"bits": m.group(1), "msb": msb, "lsb": lsb, "name": nm, "reset_text": " ".join(init),
                   "access": FOOTNOTE_RE.sub("", " ".join(rw)).strip(), "desc": [" ".join(desc)] if desc else []}
            fields.append(cur)
            continue
        if cur is None:
            continue
        first = len(l) - len(l.lstrip())
        if first < cols["name"] - 1:            # back at the left margin: table is over
            if re.match(r"^\s*(Note|x:|CAUTION|NOTE|Remark|\d+\.\d+)", l):
                cur, cols = None, None
            continue
        toks = [(mm.start(), mm.group()) for mm in re.finditer(r"\S+", l)]
        name_part, init_part, rw_part, desc_part = _split_cells(toks, cols)
        joined = "".join(name_part)
        if joined and len(joined) <= 16 and re.fullmatch(r"[A-Za-z0-9_\[\]:]+", joined):
            cur["name"] += joined              # wrapped field name: "SSC_MODE_W"+"EN", "WDTTIME"+"[11:0]"
        if init_part and not cur["reset_text"]:
            cur["reset_text"] = " ".join(init_part)     # row pushed down by a superscript footnote
        if rw_part and not cur["access"]:
            cur["access"] = FOOTNOTE_RE.sub("", " ".join(rw_part)).strip()
        if desc_part:
            cur["desc"].append(" ".join(desc_part))
    for f in fields:
        f["name"] = re.sub(r"\s+\[", "[", re.sub(r"\s+", " ", f["name"])).strip()
        f["reserved"] = f["name"] in ("", "—", "-", "–")
        f["description"] = "\n".join(d for d in f.pop("desc") if d)
    return fields


def field_reset_bits(f: dict) -> int | None:
    """Reset value of a field as an int, or None if undefined/unparseable."""
    s = f["reset_text"].strip()
    width = f["msb"] - f["lsb"] + 1
    if re.fullmatch(r"All\s*0|0+|0+[hb]", s):
        return 0
    if re.fullmatch(r"All\s*1", s):
        return (1 << width) - 1
    if re.fullmatch(r"[01]+b?", s):
        return int(s.rstrip("b"), 2)
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.fullmatch(r"(?:H[’'])?([0-9A-Fa-f_]+)h?", s)
    if m and ("h" in s or "H" in s):
        return int(m.group(1).replace("_", ""), 16)
    return None


# ---------------------------------------------------------------- module driver
@dataclass
class ModuleData:
    number: str
    name: str
    title: str
    page_start: int
    page_end: int
    bases: list = dfield(default_factory=list)
    regs: list = dfield(default_factory=list)
    defs: list = dfield(default_factory=list)
    issues: list = dfield(default_factory=list)


def slice_section(pages: dict[int, list[str]], sec: tuple, nxt: tuple | None) -> list[tuple[int, str]]:
    """Raw lines (page, line) from heading of `sec` up to heading of `nxt`."""
    _, title_raw, p0, p1 = sec
    out = []
    started = False
    stop_title = nxt[1] if nxt else None
    stop_page = nxt[2] if nxt else p1
    for p in range(p0, max(p1, stop_page) + 1):
        for l in pages.get(p, []):
            if not started:
                if norm(l).startswith(norm(title_raw)[:40]):
                    started = True
                else:
                    continue
            elif stop_title and p >= stop_page and norm(l).startswith(norm(stop_title)[:40]):
                return out
            out.append((p, l))
    return out


def parse_module(con: sqlite3.Connection, pdf: Path, doc_id: str, number: str) -> ModuleData:
    mod = con.execute("SELECT title, page_start, page_end FROM section WHERE doc_id=? AND number=?",
                      (doc_id, number)).fetchone()
    if not mod:
        raise ValueError(f"section {number} not found in {doc_id}")
    title, p0, p1 = mod
    m = re.search(r"\(([A-Za-z0-9\-/ ]+)\)\s*$", title)
    md = ModuleData(number, (m.group(1) if m else title).strip(), title, p0, p1)
    raw = strip_running_lines(raw_page_texts(pdf, p0, p1))
    pages = {p0 + i: t.split("\n") for i, t in enumerate(raw)}
    secs = con.execute("SELECT number, CASE WHEN number IS NULL THEN title ELSE number || ' ' || title END, "
                       "page_start, page_end FROM section WHERE doc_id=? AND (number=? OR number LIKE ?) ORDER BY ord",
                       (doc_id, number, number + ".%")).fetchall()
    # register descriptions = "(ABBR)" sections numbered under a "Registers" / "Register Descriptions" heading
    parents = [s[0] for s in secs if s[0] and re.fullmatch(
        r"Registers?(\s+Descriptions?)?|Register\s+(Configuration|List)", s[1].split(" ", 1)[1].strip())]
    reg_secs = [i for i, s in enumerate(secs) if s[0] and TITLE_ABBR_RE.search(s[1])
                and any(s[0].startswith(p + ".") for p in parents)]
    if not reg_secs:
        md.issues.append(("structure", "no register description sections found"))
        return md
    # overview region = from first "Register..." heading to first description heading
    ov_start = next(j for j, s in enumerate(secs) if s[0] == parents[0])
    overview = slice_section(pages, secs[ov_start], secs[reg_secs[0]])
    md.bases = parse_base_table(overview)
    channels = sorted({b["channel"] for b in md.bases if b["channel"]})
    md.regs = parse_register_list(overview, channels)

    for k, i in enumerate(reg_secs):
        sec = secs[i]
        nxt = secs[i + 1] if i + 1 < len(secs) else None
        body = slice_section(pages, sec, nxt)
        titl = sec[1]
        abbrs = TITLE_ABBR_RE.findall(titl)
        template = abbrs[-1] if abbrs else titl
        params = {v: expand_values(vals) for v, vals in PARAM_RE.findall(titl)}
        lines = [l for _, l in body]
        summary = []
        for l in lines[1:]:
            if re.match(r"^\s*(Bit\s|Access Size|Remark|Address\s*:)", l.strip()) or "Bit Name" in l:
                break
            if l.strip():
                summary.append(l.strip())
        notes, block = [], None
        for j, l in enumerate(lines):
            st = l.strip()
            if re.match(r"^(Note\b|Notes\b|CAUTION\b|NOTE\b|Remark\b)", st):
                block = [st]
                notes.append(block)
            elif block is not None:
                if st:
                    block.append(st)
                elif len(block) > 1:           # a bare "CAUTION" header may be followed by a blank line
                    nxt = next((x.strip() for x in lines[j + 1:] if x.strip()), "")
                    if not re.match(r"^(Note\b|●|•|-\s)", nxt):
                        block = None
        meta = {}
        for l in lines[:40]:
            mm = re.match(r"^\s*(Access Size|Address|Initial Value)\s*:\s*(.+?)\s*$", l)
            if mm:
                meta[mm.group(1)] = mm.group(2)
        md.defs.append({"template": template, "title": titl.split(" ", 1)[1] if sec[0] else titl,
                        "section": sec[0], "params": params, "page_start": sec[2], "page_end": sec[3],
                        "summary": re.sub(r"\s+", " ", " ".join(summary))[:600], "fields": parse_field_table(lines), "meta": meta,
                        "has_field_table": any("Bit Name" in l for l in lines),
                        "notes": "\n".join(re.sub(r"\s+", " ", " ".join(b)) for b in notes)[:2500]})
    return md


def instance_names(template: str, params: dict[str, list[str]]) -> list[str]:
    names = [template]
    for var, vals in params.items():
        names = [re.sub(rf"(?<=[A-Z0-9_]){var}(?=[A-Z0-9_]|$)|^{var}(?=[A-Z0-9_])", v, n) for n in names for v in vals]
    return names


def validate(md: ModuleData) -> None:
    by_name = {}
    for r in md.regs:
        by_name.setdefault(r["name"], []).append(r)
    defined = set()
    for d in md.defs:
        names = instance_names(d["template"], d["params"])
        d["instances"] = [n for n in names if n in by_name]
        defined.update(d["instances"])
        if not d["instances"] and d["template"] in by_name:
            d["instances"] = [d["template"]]
            defined.add(d["template"])
        sizes = {r["size"] for n in d["instances"] for r in by_name[n]}
        width = max(sizes) if sizes else (max(f["msb"] for f in d["fields"]) + 1 if d["fields"] else None)
        m = re.match(r"(\d+)", d["meta"].get("Access Size", ""))
        if m and sizes and int(m.group(1)) not in sizes:
            md.issues.append((d, None, "size_mismatch", f"description says {m.group(1)} bits, register list says {sorted(sizes)}"))
        if not d["fields"]:
            if d["has_field_table"]:
                md.issues.append((d, None, "no_fields", "bit field table present but not parsed"))
            continue
        # V0 field names look like identifiers (catches text bleeding in from headers / descriptions)
        for f in d["fields"]:
            if not f["reserved"] and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\[\d+(:\d+)?\])?", f["name"]):
                md.issues.append((d, None, "field_name", f"suspicious field name {f['name']!r} at bit(s) {f['bits']}"))
        # V1 coverage / overlap
        used = {}
        for f in d["fields"]:
            for b in range(f["lsb"], f["msb"] + 1):
                if b in used:
                    md.issues.append((d, None, "overlap", f"bit {b} in both {used[b] or 'reserved'} and {f['name'] or 'reserved'}"))
                    break
                used[b] = f["name"]
        if width:
            missing = [b for b in range(width) if b not in used]
            extra = [b for b in used if b >= width]
            if missing:
                md.issues.append((d, None, "coverage", f"bits not described: {_ranges(missing)} (width {width})"))
            if extra:
                md.issues.append((d, None, "coverage", f"bits beyond register width {width}: {_ranges(sorted(extra))}"))
        # V2 reset composition vs register list
        composed, known = 0, True
        for f in d["fields"]:
            v = field_reset_bits(f)
            if v is None:
                known = False
                break
            composed |= (v & ((1 << (f["msb"] - f["lsb"] + 1)) - 1)) << f["lsb"]
        for n in d["instances"]:
            for r in by_name[n]:
                if known and r["reset_value"] is not None and r["reset_value"] != composed:
                    md.issues.append((d, r, "reset_mismatch",
                                      f"{r['name']}: list says {r['reset_text']}, bit table composes to H'{composed:0{max(1, r['size'] // 4)}X}"))
        # V3 description meta vs list
        ad = d["meta"].get("Address")
        if ad and d["instances"]:
            mm = re.search(rf"\+\s*({HEX}h)", ad)
            if mm:
                offs = {r["offset"] for n in d["instances"] for r in by_name[n]}
                if parse_hex(mm.group(1)) not in offs and len(d["instances"]) == 1:
                    md.issues.append((d, None, "offset_mismatch", f"description address {ad}, list offset(s) {[hex(o) for o in offs]}"))
    for name, rs in by_name.items():
        if name not in defined:
            for r in rs:
                md.issues.append((None, r, "no_description", f"{name} is in the register list but no description section matched"))
    # V4 offsets: alignment + duplicates
    seen = {}
    for r in md.regs:
        if r["offset"] % max(1, r["size"] // 8):
            md.issues.append((None, r, "alignment", f"{r['name']} offset {r['offset_text']} not aligned to {r['size']}-bit access"))
        key = (r["channel"], r["offset"])
        if key in seen:
            md.issues.append((None, r, "info_shared_offset", f"{r['name']} shares offset {r['offset_text']} with {seen[key]} (check notes: may be intentional)"))
        seen[key] = r["name"]
    if not md.bases:
        md.issues.append((None, None, "no_base", "base address table not parsed"))


def _ranges(bits: list[int]) -> str:
    bits = sorted(bits)
    out, s = [], bits[0]
    for a, b in zip(bits, bits[1:] + [None]):
        if b != a + 1:
            out.append(f"{a}:{s}" if a != s else f"{a}")
            s = b
    return ",".join(reversed(out))


def store(con: sqlite3.Connection, doc_id: str, md: ModuleData) -> None:
    cur = con.execute("INSERT INTO module (doc_id, name, title, section_number, page_start, page_end) VALUES (?,?,?,?,?,?)",
                      (doc_id, md.name, md.title, md.number, md.page_start, md.page_end))
    mid = cur.lastrowid
    for b in md.bases:
        con.execute("INSERT INTO module_base VALUES (?,?,?,?,?,?)", (mid, b["channel"], b["space"], b["addr"], b["text"], b["page"]))
    def_ids = {}
    for d in md.defs:
        c = con.execute("INSERT INTO register_def (module_id, template, title, params, section_number, page_start, page_end, summary, notes)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (mid, d["template"], d["title"], "; ".join(f"{k} = {', '.join(v)}" for k, v in d["params"].items()) or None,
                         d["section"], d["page_start"], d["page_end"], d["summary"], d["notes"] or None))
        def_ids[id(d)] = c.lastrowid
        for o, f in enumerate(d["fields"]):
            con.execute("INSERT INTO field (def_id, ord, name, msb, lsb, access, reset_text, description) VALUES (?,?,?,?,?,?,?,?)",
                        (c.lastrowid, o, None if f["reserved"] else f["name"], f["msb"], f["lsb"], f["access"], f["reset_text"], f["description"]))
    inst2def = {n: d for d in md.defs for n in d.get("instances", [])}
    reg_ids = {}
    for r in md.regs:
        d = inst2def.get(r["name"])
        c = con.execute("INSERT INTO register (module_id, def_id, name, full_name, channel, offset, offset_text, size, access, reset_text, reset_value, page)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (mid, def_ids.get(id(d)) if d else None, r["name"], r["full_name"], r["channel"], r["offset"], r["offset_text"],
                         r["size"], r["access"], r["reset_text"], r["reset_value"], r["page"]))
        reg_ids[id(r)] = c.lastrowid
        fields = d["fields"] if d else []
        con.execute("INSERT INTO register_fts (rowid, name, full_name, fields, body) VALUES (?,?,?,?,?)",
                    (c.lastrowid, f"{r['name']} {d['template'] if d else ''} {md.name}", r["full_name"] or "",
                     " ".join(f["name"] for f in fields if not f["reserved"]),
                     (d["summary"] if d else "") + "\n" + "\n".join(f["description"] for f in fields)
                     + "\n" + ((d["notes"] or "") if d else "")))
    for it in md.issues:
        if len(it) == 2:
            con.execute("INSERT INTO register_issue VALUES (?,?,?,?,?)", (mid, None, None, *it))
        else:
            d, r, rule, msg = it
            con.execute("INSERT INTO register_issue VALUES (?,?,?,?,?)",
                        (mid, def_ids.get(id(d)) if d else None, reg_ids.get(id(r)) if r else None, rule, msg))


def build(db: Path) -> None:
    m = catalog.load()
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for d in m["documents"]:
        for number in d.get("register_modules", []):
            md = parse_module(con, ROOT / d["path"], d["id"], number)
            validate(md)
            store(con, d["id"], md)
            nf = sum(len(x["fields"]) for x in md.defs)
            print(f"  {d['id']} {number:<5} {md.name:<6} bases={len(md.bases):<3} regs={len(md.regs):<4} "
                  f"defs={len(md.defs):<4} fields={nf:<5} issues={len(md.issues)}", file=sys.stderr)
    con.commit()
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    build(ap.parse_args().db)
