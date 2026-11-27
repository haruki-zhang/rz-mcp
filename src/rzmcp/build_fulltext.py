"""Layer A: catalog + section tree + page text + full-text index for every manifest document.

    PYTHONPATH=src python3 -m rzmcp.build_fulltext [--db data/rz.db]

Rebuilds the layer-A tables from scratch (layer-B register tables are left untouched).
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import DB_PATH, ROOT, catalog, pinfunc
from .pdf import compact, norm, outline, raw_page_texts, strip_running_lines

SCHEMA = """
DROP TABLE IF EXISTS chunk_fts;
DROP TABLE IF EXISTS chunk;
DROP TABLE IF EXISTS section;
DROP TABLE IF EXISTS page;
DROP TABLE IF EXISTS bom_item;
DROP TABLE IF EXISTS document_device;
DROP TABLE IF EXISTS document_board;
DROP TABLE IF EXISTS board_device;
DROP TABLE IF EXISTS board;
DROP TABLE IF EXISTS device;
DROP TABLE IF EXISTS document;

CREATE TABLE device   (device TEXT PRIMARY KEY, name TEXT NOT NULL, family TEXT, note TEXT);
CREATE TABLE board    (board TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT);
CREATE TABLE board_device (board TEXT NOT NULL, device TEXT NOT NULL, PRIMARY KEY (board, device));
CREATE TABLE document (
    doc_id TEXT PRIMARY KEY,           -- official document / drawing number
    title TEXT NOT NULL, doc_type TEXT NOT NULL, rev TEXT NOT NULL,
    path TEXT NOT NULL, pages INTEGER, confidentiality TEXT, note TEXT
);
CREATE TABLE document_device (doc_id TEXT NOT NULL, device TEXT NOT NULL, PRIMARY KEY (doc_id, device));
CREATE TABLE document_board  (doc_id TEXT NOT NULL, board  TEXT NOT NULL, PRIMARY KEY (doc_id, board));
CREATE TABLE page (doc_id TEXT NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY (doc_id, page));
CREATE TABLE section (
    section_id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL, ord INTEGER NOT NULL,
    number TEXT,                       -- "5.4.3.1"; NULL for un-numbered bookmarks
    title TEXT NOT NULL, level INTEGER NOT NULL, parent_id INTEGER,
    page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
    path TEXT NOT NULL                 -- breadcrumb of ancestor headings
);
CREATE INDEX section_doc_number ON section (doc_id, number);
CREATE TABLE chunk (                   -- one section's own text on one page (or one page / BOM row)
    chunk_id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL, section_id INTEGER,
    page INTEGER, locator TEXT NOT NULL,
    title TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL
);
CREATE INDEX chunk_section ON chunk (section_id);
CREATE TABLE bom_item (
    doc_id TEXT NOT NULL, row INTEGER NOT NULL, qty TEXT, category TEXT, part_number TEXT, value TEXT,
    description TEXT, refdes TEXT, manufacturer TEXT, mpn TEXT, PRIMARY KEY (doc_id, row)
);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    title, path, body, content='chunk', content_rowid='chunk_id', tokenize='porter unicode61'
);
"""

SEC_RE = re.compile(r"^(?:SECTION\s+)?(\d+(?:\.\d+)*)\.?\s+(.*)$")


def split_number(title: str) -> tuple[str | None, str]:
    m = SEC_RE.match(title)
    return (m.group(1), m.group(2).strip()) if m else (None, title)


def locate_heading(lines: list[str], title: str, start: int) -> int | None:
    """Index of the line that starts the heading `title`, searching from line `start`."""
    want = norm(title)[:40]
    if not want:
        return None
    for i in range(start, len(lines)):
        got = norm(lines[i])
        if got and (got.startswith(want) or (len(got) >= 12 and want.startswith(got))):
            return i
    return None


def first_content_line(lines: list[str]) -> int:
    return next((i for i, l in enumerate(lines) if l.strip()), 0)


def build_sections(doc_id: str, items: list[dict], pages: list[list[str]], profile: str):
    """Turn bookmarks into sections with page ranges and per-page own-text pieces.

    Returns (sections, pieces): sections are dicts; pieces are (section_idx|None, page, text).
    """
    n_pages = len(pages)
    if profile == "schematic":
        items = [it for it in items if it["group"] == "Design Hierarchy" and it["page"] and it["level"] >= 3]
        items = [dict(it, level=1) for it in items]
    else:
        # Word exports leave empty-heading bookmarks like "1." / "1.1": drop letterless titles
        items = [it for it in items if it["page"] and 1 <= it["page"] <= n_pages
                 and re.search(r"[A-Za-z]", it["title"])]

    # locate each heading on its page; keep document order
    secs, cursor = [], {}
    for it in items:
        p = it["page"]
        lines = pages[p - 1]
        start = cursor.get(p, 0)
        idx = locate_heading(lines, it["title"], start) if profile != "schematic" else None
        if idx is None:
            idx = start if start else first_content_line(lines)
            found = False
        else:
            found = True
        cursor[p] = idx + 1 if found else start
        number, title = split_number(it["title"]) if profile != "schematic" else (None, it["title"])
        secs.append({"level": it["level"], "raw": it["title"], "number": number, "title": title,
                     "page": p, "line": idx})
    # enforce monotonic positions (bookmark order is authoritative)
    for a, b in zip(secs, secs[1:]):
        if (b["page"], b["line"]) < (a["page"], a["line"]):
            b["page"], b["line"] = a["page"], a["line"]

    # parents, breadcrumbs, page ranges
    stack = []
    for i, s in enumerate(secs):
        while stack and secs[stack[-1]]["level"] >= s["level"]:
            stack.pop()
        s["parent"] = stack[-1] if stack else None
        s["path"] = " > ".join(secs[k]["raw"] for k in stack)
        stack.append(i)
    for i, s in enumerate(secs):
        nxt = next((secs[j] for j in range(i + 1, len(secs)) if secs[j]["level"] <= s["level"]), None)
        if nxt is None:
            end = n_pages
        else:
            end = nxt["page"]
            if nxt["line"] <= first_content_line(pages[nxt["page"] - 1]) and end > s["page"]:
                end -= 1
        s["page_end"] = max(s["page"], end)

    # own-text pieces, split per page
    pieces = []
    first = (secs[0]["page"], secs[0]["line"]) if secs else (n_pages + 1, 0)
    for p in range(1, first[0]):                      # front matter before the first bookmark
        pieces.append((None, p, "\n".join(pages[p - 1])))
    if secs and first[1] > 0:
        pieces.append((None, first[0], "\n".join(pages[first[0] - 1][:first[1]])))
    for i, s in enumerate(secs):
        end = (secs[i + 1]["page"], secs[i + 1]["line"]) if i + 1 < len(secs) else (n_pages, len(pages[-1]))
        for p in range(s["page"], end[0] + 1):
            lines = pages[p - 1]
            lo = s["line"] if p == s["page"] else 0
            hi = end[1] if p == end[0] else len(lines)
            if i + 1 == len(secs) and p == end[0]:
                hi = len(lines)
            txt = "\n".join(lines[lo:hi])
            if txt.strip():
                pieces.append((i, p, txt))
    return secs, pieces


def index_pdf(con: sqlite3.Connection, d: dict, profile: str) -> tuple[int, int]:
    pdf = ROOT / d["path"]
    pages_txt = [compact(t) for t in strip_running_lines(raw_page_texts(pdf))]
    con.executemany("INSERT INTO page VALUES (?,?,?)", [(d["id"], i + 1, t) for i, t in enumerate(pages_txt)])
    lines = [t.split("\n") for t in pages_txt]
    items = outline(pdf) if profile in ("bookmarks", "schematic") else []
    secs, pieces = build_sections(d["id"], items, lines, profile)

    ids = []
    for ord_, s in enumerate(secs):
        parent_id = ids[s["parent"]] if s["parent"] is not None else None
        cur = con.execute(
            "INSERT INTO section (doc_id, ord, number, title, level, parent_id, page_start, page_end, path)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (d["id"], ord_, s["number"], s["title"], s["level"], parent_id, s["page"], s["page_end"], s["path"]))
        ids.append(cur.lastrowid)
    for si, p, txt in pieces:
        if si is None:
            # no bookmark: empty FTS title (the document title on every page would drown the body text);
            # the query layer displays "<document title> page N" instead
            title, path, sid = "", "", None
        else:
            s = secs[si]
            title, path, sid = s["raw"], s["path"], ids[si]
        con.execute("INSERT INTO chunk (doc_id, section_id, page, locator, title, path, body) VALUES (?,?,?,?,?,?,?)",
                    (d["id"], sid, p, f"p.{p}", title, path, txt))
    return len(pages_txt), len(secs)


BOM_COLS = {"qty": "Qty", "category": "CATEGORY", "part_number": "PART_NUMBER", "value": "VALUE",
            "description": "DESCRIPTION", "refdes": "Ref Des", "manufacturer": "MANUFACTURER_PRIMARY",
            "mpn": "MANUFACTURER_PRIMARY_ORDER_CODE"}


def index_bom(con: sqlite3.Connection, d: dict) -> int:
    path = ROOT / d["path"]
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    hdr = next(i for i, l in enumerate(lines) if l.startswith("Qty,"))
    rows = list(csv.DictReader(lines[hdr:]))
    boards = ", ".join(d.get("boards", []))
    for n, r in enumerate(rows, 1):
        v = {k: (r.get(col) or "").strip() for k, col in BOM_COLS.items()}
        if not any(v.values()):
            continue
        con.execute("INSERT INTO bom_item VALUES (?,?,?,?,?,?,?,?,?,?)", (d["id"], n, *v.values()))
        body = "\n".join(f"{col}: {v[k]}" for k, col in BOM_COLS.items() if v[k])
        con.execute("INSERT INTO chunk (doc_id, section_id, page, locator, title, path, body) VALUES (?,?,?,?,?,?,?)",
                    (d["id"], None, None, f"BOM row {n}", f"BOM {boards}: {v['description']} {v['value']}".strip(),
                     d["title"], body))
    return len(rows)


def index_pinfunction(con: sqlite3.Connection, d: dict) -> int:
    recs = pinfunc.records(ROOT / d["path"])
    con.executemany("INSERT INTO chunk (doc_id, section_id, page, locator, title, path, body) VALUES (?,?,?,?,?,?,?)",
                    [(d["id"], None, None, r["locator"], r["title"], d["title"], r["body"]) for r in recs])
    return len(recs)


def build(db: Path) -> None:
    m = catalog.load()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for dev, v in m.get("devices", {}).items():
        con.execute("INSERT INTO device VALUES (?,?,?,?)", (dev, v["name"], v.get("family"), v.get("note")))
    for b, v in m.get("boards", {}).items():
        con.execute("INSERT INTO board VALUES (?,?,?)", (b, v["name"], v.get("note")))
        con.executemany("INSERT INTO board_device VALUES (?,?)", [(b, x) for x in v.get("devices", [])])
    t0 = time.time()
    for d in m["documents"]:
        con.execute("INSERT INTO document VALUES (?,?,?,?,?,?,?,?)",
                    (d["id"], d["title"], d["doc_type"], d["rev"], d["path"], d.get("pages"),
                     d.get("confidentiality"), d.get("note")))
        con.executemany("INSERT INTO document_device VALUES (?,?)", [(d["id"], x) for x in d["devices"]])
        con.executemany("INSERT INTO document_board VALUES (?,?)", [(d["id"], x) for x in d.get("boards", [])])
        idx = d.get("index", [])
        if "fulltext" in idx and d["path"].lower().endswith(".pdf"):
            np_, ns = index_pdf(con, d, d.get("outline", "none"))
            print(f"  {d['id']:<22} {np_:>5} pages {ns:>5} sections", file=sys.stderr)
        if "pinfunction" in idx:
            print(f"  {d['id']:<22} {index_pinfunction(con, d):>5} pin records", file=sys.stderr)
        if "bom" in idx:
            print(f"  {d['id']:<22} {index_bom(con, d):>5} BOM rows", file=sys.stderr)
    con.execute("INSERT INTO chunk_fts (chunk_fts) VALUES ('rebuild')")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
    print(f"layer A built: {n} chunks in {time.time() - t0:.1f}s -> {db}", file=sys.stderr)
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    build(ap.parse_args().db)
