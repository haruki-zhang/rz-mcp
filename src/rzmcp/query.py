"""Query layer behind the MCP tools. Pure stdlib (sqlite3); every answer carries its source.

All functions return compact plain text meant to be read by an LLM agent. They never guess:
when something is not in the database they say so and point at the next tool to try.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from . import DB_PATH

MAX_OUT = 12000          # hard cap on any single tool response (chars)
MAX_PAGES = 5
STOP = {"a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "is", "are", "what", "which", "how",
        "does", "do", "be", "with", "by", "at", "from", "this", "that", "it", "as", "can", "i", "where",
        "when", "about", "into", "there", "should", "value", "values"}

_local = threading.local()
_db_path = DB_PATH


def set_db(path: Path) -> None:
    global _db_path
    _db_path = path


def db() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(f"file:{_db_path}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        _local.con = con
    return con


# ------------------------------------------------------------------ helpers
def _cap(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[output truncated at {limit} chars]"


def _check_device(device: str) -> str | None:
    rows = db().execute("SELECT DISTINCT device FROM document_device ORDER BY device").fetchall()
    known = [r[0] for r in rows]
    if device not in known:
        return f"Unknown device {device!r}. Known device ids: {', '.join(known)}."
    return None


def _doc(doc_id: str) -> sqlite3.Row | None:
    return db().execute("SELECT * FROM document WHERE upper(doc_id)=upper(?)", (doc_id.strip(),)).fetchone()


def _default_hw_manual(device: str) -> str | None:
    r = db().execute("SELECT d.doc_id FROM document d JOIN document_device dd USING(doc_id) "
                     "WHERE dd.device=? AND d.doc_type='hw_manual' ORDER BY d.doc_id LIMIT 1", (device,)).fetchone()
    return r[0] if r else None


def _cite(doc: sqlite3.Row, section: str | None = None, pages: str | None = None) -> str:
    s = f"{doc['doc_id']} Rev {doc['rev']}"
    if section:
        s += f" §{section}"
    if pages:
        s += f" {pages}"
    return s


def _pages(a: int, b: int) -> str:
    return f"p.{a}" if a == b else f"pp.{a}-{b}"


def _term(t: str) -> str:
    """Register-like tokens also match the manual's template spelling: GTCCRD -> GTCCRn / GTCCRm / GTCCR*."""
    if len(t) >= 5 and re.fullmatch(r"[A-Z][A-Z0-9]+", t) and sum(c.isalpha() for c in t) >= 3:
        stem = t[:-1]
        return f'("{t}" OR "{stem}n" OR "{stem}m" OR "{stem}"*)'
    return f'"{t}"'


def fts_query(q: str, mode: str = "and") -> str | None:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q) if t.lower() not in STOP]
    if not toks:
        return None
    terms = [_term(t) for t in dict.fromkeys(toks)]
    return (" AND " if mode == "and" else " OR ").join(terms)   # FTS5 needs explicit AND next to (...) groups


# ------------------------------------------------------------------ documents
def list_documents(device: str = "rzg3l", doc_type: str | None = None) -> str:
    if err := _check_device(device):
        return err
    sql = ("SELECT d.* FROM document d JOIN document_device dd USING(doc_id) WHERE dd.device=?"
           + (" AND d.doc_type=?" if doc_type else "") + " ORDER BY d.doc_type, d.doc_id")
    rows = db().execute(sql, (device, doc_type) if doc_type else (device,)).fetchall()
    out = [f"{len(rows)} documents for {device}" + (f" (type {doc_type})" if doc_type else "") + ":",
           "doc_id | type | rev | pages | title | other devices"]
    for d in rows:
        devs = [r[0] for r in db().execute("SELECT device FROM document_device WHERE doc_id=? AND device!=?",
                                            (d["doc_id"], device))]
        out.append(f"{d['doc_id']} | {d['doc_type']} | {d['rev']} | {d['pages'] or '-'} | {d['title']}"
                   + (f" | also: {', '.join(devs)}" if devs else ""))
        if d["note"]:
            out.append(f"    note: {d['note']}")
    mods = db().execute("SELECT m.name, m.section_number, m.doc_id, (SELECT COUNT(*) FROM register r WHERE r.module_id=m.module_id) n "
                        "FROM module m JOIN document_device dd USING(doc_id) WHERE dd.device=? ORDER BY m.section_number",
                        (device,)).fetchall()
    if mods:
        out.append("\nStructured register data (lookup_register / search_registers) is available for: "
                   + ", ".join(f"{m['name']} (§{m['section_number']}, {m['n']} registers)" for m in mods)
                   + ". Other modules: use search_docs / get_section on the hardware manual.")
    return _cap("\n".join(out))


def search_docs(query: str, device: str = "rzg3l", doc_type: str | None = None, doc_id: str | None = None,
                limit: int = 8) -> str:
    if err := _check_device(device):
        return err
    limit = max(1, min(int(limit), 20))
    hits, seen = [], set()
    for mode in ("and", "or"):
        q = fts_query(query, mode)
        if not q:
            return "Query has no searchable words. Use English keywords (the documents are in English)."
        sql = ("SELECT c.chunk_id, c.doc_id, c.section_id, c.page, c.locator, c.title, c.path, "
               "snippet(chunk_fts, 2, '«', '»', ' … ', 18) AS snip, bm25(chunk_fts, 10.0, 2.0, 1.0) AS rank "
               "FROM chunk_fts JOIN chunk c ON c.chunk_id = chunk_fts.rowid "
               "JOIN document_device dd ON dd.doc_id = c.doc_id AND dd.device = ? "
               "JOIN document d ON d.doc_id = c.doc_id WHERE chunk_fts MATCH ?")
        args: list = [device, q]
        if doc_type:
            sql += " AND d.doc_type = ?"
            args.append(doc_type)
        if doc_id:
            sql += " AND upper(c.doc_id) = upper(?)"
            args.append(doc_id)
        sql += " ORDER BY rank LIMIT 200"
        for r in db().execute(sql, args):
            key = r["section_id"] or ("chunk", r["chunk_id"])
            if key in seen:
                continue
            seen.add(key)
            hits.append((mode, r))
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if not hits:
        return (f"No matches for {query!r} in documents for {device}. Try other keywords, a register/module "
                "abbreviation, or list_documents to see what is indexed.")
    out = [f"Results for {query!r} (device {device}):"]
    for i, (mode, r) in enumerate(hits, 1):
        doc = _doc(r["doc_id"])
        sec = db().execute("SELECT number, page_start, page_end FROM section WHERE section_id=?",
                           (r["section_id"],)).fetchone() if r["section_id"] else None
        where = f"§{sec['number']} " if sec and sec["number"] and not r["title"].startswith(sec["number"]) else ""
        rng = f" (section {_pages(sec['page_start'], sec['page_end'])})" if sec else ""
        out.append(f"[{i}] {doc['doc_id']} Rev {doc['rev']} [{doc['doc_type']}] {where}{r['title']} — hit at {r['locator']}{rng}"
                   + (" [partial match]" if mode == "or" else ""))
        if r["path"]:
            out.append(f"    in: {r['path']}")
        out.append("    " + re.sub(r"\s+", " ", r["snip"]).strip())
    out.append("\nNext: get_section(doc_id, section) for a full section, read_pages(doc_id, start, end) for raw pages.")
    return _cap("\n".join(out))


def _find_section(doc_id: str, section: str) -> tuple[list[sqlite3.Row], str | None]:
    s = section.strip().removeprefix("§").strip()
    s = re.sub(r"^(section|chapter)\s+", "", s, flags=re.I)
    rows = db().execute("SELECT * FROM section WHERE doc_id=? AND number=? ORDER BY ord", (doc_id, s.rstrip("."))).fetchall()
    if rows:
        return rows, None
    rows = db().execute("SELECT * FROM section WHERE doc_id=? AND lower(title) LIKE lower(?) ORDER BY level, ord LIMIT 10",
                        (doc_id, f"%{s}%")).fetchall()
    return rows, None if rows else f"Section {section!r} not found in {doc_id}."


def get_toc(doc_id: str | None = None, under: str | None = None, depth: int = 1, device: str = "rzg3l") -> str:
    doc_id = doc_id or _default_hw_manual(device)
    doc = _doc(doc_id) if doc_id else None
    if not doc:
        return f"Unknown document {doc_id!r}. Use list_documents."
    depth = max(1, min(int(depth), 4))
    if under:
        rows, err = _find_section(doc["doc_id"], under)
        if err:
            return err + " Call get_toc without 'under' to see top-level sections."
        root = rows[0]
        nxt = db().execute("SELECT MIN(ord) FROM section WHERE doc_id=? AND ord>? AND level<=?",
                           (doc["doc_id"], root["ord"], root["level"])).fetchone()[0]
        lo, hi, base = root["ord"] + 1, nxt if nxt is not None else 10 ** 9, root["level"]
        head = f"Sections under §{root['number'] or ''} {root['title']} ({_cite(doc)}):"
    else:
        lo, hi, base = 0, 10 ** 9, 0
        head = f"Table of contents of {_cite(doc)} {doc['title']}:"
    rows = db().execute("SELECT number, title, level, page_start, page_end FROM section WHERE doc_id=? AND ord>=? AND ord<? "
                        "AND level<=? ORDER BY ord", (doc["doc_id"], lo, hi, base + depth)).fetchall()
    if not rows:
        return head + "\n(no subsections)"
    minlvl = min(r["level"] for r in rows)
    out = [head] + [f"{'  ' * (r['level'] - minlvl)}{(r['number'] + ' ') if r['number'] else ''}{r['title']}  "
                    f"[{_pages(r['page_start'], r['page_end'])}]" for r in rows[:300]]
    if len(rows) > 300:
        out.append(f"… {len(rows) - 300} more; narrow with 'under'.")
    return _cap("\n".join(out))


def get_section(section: str, doc_id: str | None = None, device: str = "rzg3l", offset: int = 0,
                max_chars: int = 8000) -> str:
    doc_id = doc_id or _default_hw_manual(device)
    doc = _doc(doc_id) if doc_id else None
    if not doc:
        return f"Unknown document {doc_id!r}. Use list_documents."
    rows, err = _find_section(doc["doc_id"], section)
    if err:
        return err + " Use get_toc or search_docs to find the right section."
    sec = rows[0]
    nxt = db().execute("SELECT MIN(ord) FROM section WHERE doc_id=? AND ord>? AND level<=?",
                       (doc["doc_id"], sec["ord"], sec["level"])).fetchone()[0]
    hi = nxt if nxt is not None else 10 ** 9
    chunks = db().execute("SELECT c.page, c.body FROM chunk c JOIN section s ON s.section_id=c.section_id "
                          "WHERE s.doc_id=? AND s.ord>=? AND s.ord<? ORDER BY c.chunk_id",
                          (doc["doc_id"], sec["ord"], hi)).fetchall()
    parts, last = [], None
    for c in chunks:
        if c["page"] != last:
            parts.append(f"[p.{c['page']}]")
            last = c["page"]
        parts.append(c["body"])
    text = "\n".join(parts)
    max_chars = max(1000, min(int(max_chars), MAX_OUT - 1500))
    offset = max(0, int(offset))
    body = text[offset:offset + max_chars]
    label = f"§{sec['number']} {sec['title']}" if sec["number"] else sec["title"]
    out = [f"{label}", f"Source: {_cite(doc, sec['number'], _pages(sec['page_start'], sec['page_end']))} — {doc['title']}"]
    if sec["path"]:
        out.append(f"In: {sec['path']}")
    if len(rows) > 1:
        out.append("Other sections matching this reference: " + "; ".join(
            f"{(r['number'] + ' ') if r['number'] else ''}{r['title']} [p.{r['page_start']}]" for r in rows[1:6]))
    kids = db().execute("SELECT number, title, page_start FROM section WHERE doc_id=? AND parent_id=? ORDER BY ord",
                        (doc["doc_id"], sec["section_id"])).fetchall()
    if kids and offset == 0:
        out.append("Subsections: " + "; ".join(f"{k['number'] or ''} {k['title']} [p.{k['page_start']}]".strip() for k in kids[:40])
                   + (f"; … {len(kids) - 40} more (get_toc)" if len(kids) > 40 else ""))
    out.append("-" * 40)
    out.append(body if body.strip() else "(no text)")
    if offset + max_chars < len(text):
        out.append(f"\n…[{len(text) - offset - max_chars} more chars; call get_section again with offset={offset + max_chars}, "
                   "or use get_toc(under=...) to pick a subsection]")
    return "\n".join(out)


def read_pages(doc_id: str, start_page: int, end_page: int | None = None) -> str:
    doc = _doc(doc_id)
    if not doc:
        return f"Unknown document {doc_id!r}. Use list_documents."
    start_page = int(start_page)
    end_page = int(end_page or start_page)
    if end_page < start_page:
        start_page, end_page = end_page, start_page
    note = ""
    if end_page - start_page + 1 > MAX_PAGES:
        end_page = start_page + MAX_PAGES - 1
        note = f"\n…[limited to {MAX_PAGES} pages per call]"
    rows = db().execute("SELECT page, text FROM page WHERE doc_id=? AND page BETWEEN ? AND ? ORDER BY page",
                        (doc["doc_id"], start_page, end_page)).fetchall()
    if not rows:
        return f"No pages {start_page}-{end_page} in {doc['doc_id']} (document has {doc['pages']} pages, text-indexed: " \
               f"{'yes' if db().execute('SELECT 1 FROM page WHERE doc_id=? LIMIT 1', (doc['doc_id'],)).fetchone() else 'no'})."
    out = [f"=== {_cite(doc)} {doc['title']} — page {r['page']} of {doc['pages']} ===\n{r['text']}" for r in rows]
    return _cap("\n\n".join(out)) + note


# ------------------------------------------------------------------ registers
def _parse_addr(q: str) -> int | None:
    s = q.strip()
    m = re.fullmatch(r"(?:0x|H[’']|h')?\s*([0-9A-Fa-f_]+)\s*h?", s)
    if not m or not re.search(r"\d", s):
        return None
    if not (s.lower().startswith(("0x", "h'", "h’")) or s.lower().endswith("h") or len(m.group(1).replace("_", "")) >= 6):
        return None
    return int(m.group(1).replace("_", ""), 16)


def _fmt_addr(a: int) -> str:
    h = f"{a:08X}"
    return f"0x{h[:-4]}_{h[-4:]}"


def _bases(module_id: int, channel: str | None) -> list[sqlite3.Row]:
    if channel is None:
        return db().execute("SELECT * FROM module_base WHERE module_id=? ORDER BY channel, address_space", (module_id,)).fetchall()
    return db().execute("SELECT * FROM module_base WHERE module_id=? AND (channel=? OR channel IS NULL) ORDER BY address_space",
                        (module_id, channel)).fetchall()


def _modules_line(device: str) -> str:
    mods = db().execute("SELECT m.name, m.section_number FROM module m JOIN document_device dd USING(doc_id) WHERE dd.device=? "
                        "ORDER BY m.section_number", (device,)).fetchall()
    return ", ".join(f"{m['name']} (§{m['section_number']})" for m in mods) or "none"


def _render_def(def_id: int | None, regs: list[sqlite3.Row], spaces: str | None) -> str:
    con = db()
    r0 = regs[0]
    mod = con.execute("SELECT * FROM module WHERE module_id=?", (r0["module_id"],)).fetchone()
    doc = _doc(mod["doc_id"])
    d = con.execute("SELECT * FROM register_def WHERE def_id=?", (def_id,)).fetchone() if def_id else None
    title = d["template"] if d else r0["name"]
    full = d["title"] if d else (r0["full_name"] or "")
    out = [f"## {title} — {full}  [module {mod['name']}]"]
    src = f"register list p.{r0['page']}"
    if d:
        src = f"description §{d['section_number']} {_pages(d['page_start'], d['page_end'])}; " + src
    out.append(f"Source: {_cite(doc)} ({src})")
    sizes = sorted({r["size"] for r in regs})
    acc = sorted({r["access"] for r in regs if r["access"]})
    resets = sorted({r["reset_text"] for r in regs if r["reset_text"]})
    out.append(f"Size: {'/'.join(map(str, sizes))} bits" + (f" | Access: {', '.join(acc)}" if acc else "")
               + f" | Reset (register list): {', '.join(resets)}")
    if d and d["params"]:
        out.append(f"Parameters: {d['params']}")
    out.append("Instances (offset → absolute address per address space):")
    want = {s.strip().upper() for s in spaces.split(",")} if spaces else None
    for r in regs:
        bases = [b for b in _bases(r["module_id"], r["channel"]) if not want or b["address_space"].upper() in want]
        if r["channel"] is None and len({b["channel"] for b in bases}) > 1:
            by_ch: dict = {}
            for b in bases:
                by_ch.setdefault(b["channel"], []).append(b)
            out.append(f"  {r['name']}  offset {r['offset_text']} (same offset in every channel)")
            for ch, bs in by_ch.items():
                out.append(f"      {ch}: " + " | ".join(f"{b['address_space']} {_fmt_addr(b['base_address'] + r['offset'])} (base {_fmt_addr(b['base_address'])})" for b in bs))
        else:
            addrs = " | ".join(f"{b['address_space']} {_fmt_addr(b['base_address'] + r['offset'])} (base {_fmt_addr(b['base_address'])})" for b in bases)
            out.append(f"  {r['name']}" + (f" [{r['channel']}]" if r["channel"] else "") + f"  offset {r['offset_text']}  → {addrs or '(base address unknown)'}"
                       + (f"  reset {r['reset_text']}" if len(resets) > 1 else ""))
    if d:
        fields = con.execute("SELECT * FROM field WHERE def_id=? ORDER BY msb DESC", (def_id,)).fetchall()
        if fields:
            out.append("Fields:")
            for f in fields:
                bits = f"[{f['msb']}]" if f["msb"] == f["lsb"] else f"[{f['msb']}:{f['lsb']}]"
                desc = re.sub(r"\n+", " / ", f["description"] or "")
                if len(desc) > 500:
                    desc = desc[:500] + " …(see section text)"
                out.append(f"  {bits:<8} {f['name'] or '(reserved)'}  {f['access'] or ''}  reset={f['reset_text']}  — {desc}")
        else:
            out.append("Fields: the manual has no bit-field table for this register (whole-register value); see the description section.")
        if d["summary"]:
            out.append(f"Summary: {d['summary']}")
        if d["notes"]:
            notes = d["notes"] if len(d["notes"]) <= 1500 else d["notes"][:1500] + " …(see section text)"
            out.append(f"Notes / cautions from the description section:\n  " + notes.replace("\n", "\n  "))
    issues = con.execute("SELECT rule, message FROM register_issue WHERE (def_id=? AND ? IS NOT NULL) OR reg_id IN (%s)"
                         % ",".join("?" * len(regs)), (def_id, def_id, *[r["reg_id"] for r in regs])).fetchall()
    real = [i for i in issues if not i["rule"].startswith("info_")]
    if real:
        out.append("⚠ Extraction check failed — verify against the page: " + "; ".join(f"{i['rule']}: {i['message']}" for i in real))
    else:
        out.append("Extraction checks: passed (bit coverage / reset value cross-checked where the manual defines them)"
                   + ("; note: " + "; ".join(i["message"] for i in issues) if issues else ""))
    return "\n".join(out)


def lookup_register(query: str, device: str = "rzg3l", module: str | None = None, address_space: str | None = None) -> str:
    if err := _check_device(device):
        return err
    con = db()
    base_sql = ("SELECT r.* FROM register r JOIN module m USING(module_id) JOIN document_device dd ON dd.doc_id=m.doc_id "
                "AND dd.device=? WHERE 1=1" + (" AND upper(m.name)=upper(?)" if module else ""))
    base_args = [device] + ([module] if module else [])
    q = query.strip()
    addr = _parse_addr(q)
    regs: list[sqlite3.Row] = []
    how = ""
    if addr is not None:
        for r in con.execute(base_sql, base_args):
            for b in _bases(r["module_id"], r["channel"]):
                a = b["base_address"] + r["offset"]
                if a <= addr < a + max(1, r["size"] // 8) and (not address_space or b["address_space"].upper() == address_space.upper()):
                    regs.append(r)
                    how = f"address {_fmt_addr(addr)} = {b['address_space']} base {_fmt_addr(b['base_address'])}" \
                          + (f" ({b['channel']})" if b["channel"] else "") + f" + offset {r['offset_text']}"
                    break
        if not regs:
            return (f"No structured register at address {_fmt_addr(addr)} for {device}. Structured modules: {_modules_line(device)}. "
                    "For other modules search the address map (search_docs 'address map') or the module chapter.")
    else:
        name = q.upper()
        for sql, arg, label in ((" AND upper(r.name)=?", name, "exact name"),
                                (" AND r.def_id IN (SELECT def_id FROM register_def WHERE upper(template)=?)", name, "template"),
                                (" AND upper(r.name) LIKE ?", name + "%", "name prefix"),
                                (" AND upper(r.name) LIKE ?", "%" + name + "%", "name substring")):
            regs = con.execute(base_sql + sql + " ORDER BY r.module_id, r.offset, r.channel", base_args + [arg]).fetchall()
            if regs:
                how = label
                break
        if not regs:
            sugg = search_registers(q, device=device, module=module, limit=5)
            return (f"No register named {q!r} in the structured data for {device} (structured modules: {_modules_line(device)}).\n"
                    f"Closest by text search:\n{sugg}\n"
                    "If the register belongs to another module, use search_docs with its name to find it in the hardware manual.")
    groups: dict = {}
    for r in regs:
        groups.setdefault((r["module_id"], r["def_id"] or ("solo", r["reg_id"])), []).append(r)
    if len(groups) > 6:
        lines = [f"{len(regs)} registers match {q!r} ({how}); showing names only — refine the query:"]
        for (mid, _), rs in list(groups.items())[:60]:
            lines.append("  " + ", ".join(r["name"] for r in rs[:8]) + (" …" if len(rs) > 8 else ""))
        return _cap("\n".join(lines))
    parts = [f"Matched by {how}:"] + [_render_def(k[1] if not isinstance(k[1], tuple) else None, rs, address_space)
                                      for k, rs in groups.items()]
    return _cap("\n\n".join(parts))


def search_registers(query: str, device: str = "rzg3l", module: str | None = None, limit: int = 12) -> str:
    if err := _check_device(device):
        return err
    limit = max(1, min(int(limit), 30))
    con = db()
    rows, seen = [], set()
    for mode in ("and", "or"):
        fq = fts_query(query, mode)
        if not fq:
            return "Query has no searchable words."
        sql = ("SELECT r.*, m.name AS module, bm25(register_fts, 10.0, 4.0, 6.0, 1.0) AS rank, "
               "snippet(register_fts, 3, '«', '»', ' … ', 12) AS snip FROM register_fts "
               "JOIN register r ON r.reg_id = register_fts.rowid JOIN module m USING(module_id) "
               "JOIN document_device dd ON dd.doc_id=m.doc_id AND dd.device=? WHERE register_fts MATCH ?"
               + (" AND upper(m.name)=upper(?)" if module else "") + " ORDER BY rank LIMIT 100")
        for r in con.execute(sql, [device, fq] + ([module] if module else [])):
            key = r["def_id"] or r["reg_id"]
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    if not rows:
        return f"No structured register matches {query!r} (structured modules: {_modules_line(device)}). Try search_docs."
    out = [f"Registers matching {query!r}:"]
    qtoks = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", query)}
    for r in rows:
        d = con.execute("SELECT template, section_number, page_start FROM register_def WHERE def_id=?", (r["def_id"],)).fetchone() \
            if r["def_id"] else None
        fields = con.execute("SELECT name, msb, lsb FROM field WHERE def_id=? AND name IS NOT NULL", (r["def_id"],)).fetchall() \
            if r["def_id"] else []
        hit_f = [f for f in fields if any(t in f["name"].lower() for t in qtoks)]
        nm = d["template"] if d else r["name"]
        out.append(f"- {nm} ({r['full_name']}) [{r['module']}] offset {r['offset_text']}, {r['size']} bits"
                   + (f" — §{d['section_number']} p.{d['page_start']}" if d else f" — list p.{r['page']}")
                   + (f"; fields: {', '.join(f['name'] + ('[%d]' % f['msb'] if f['msb'] == f['lsb'] else '[%d:%d]' % (f['msb'], f['lsb'])) for f in hit_f[:6])}" if hit_f else ""))
        snip = re.sub(r"\s+", " ", r["snip"] or "").strip()
        if snip and "«" in snip:
            out.append(f"    {snip}")
    out.append("\nUse lookup_register(name) for full details (addresses, all fields).")
    return _cap("\n".join(out))
