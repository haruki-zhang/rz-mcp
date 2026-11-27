"""Retrieval lab: compare search_docs ranking variants on the same index. Experiment only.

    PYTHONPATH=src python3 eval/retrieval_lab/lab.py [--db data/rz.db] [--alt-db /tmp/rz-alt.db] [-v]

Query sets
  smoke    : eval/smoke_questions.toml, question text as the query (pessimistic), as in run_smoke.py
  cases    : cases.toml `pos` (agent-style keyword queries, incl. ticket-derived and page-level documents)
  caption  : table / figure captions sampled from the index; query = caption words, truth = the pages
             that print the caption (known-item search)
  caption3 : the same captions cut down to 3 content words (closer to what an agent types)
  neg      : cases.toml `neg`; handled = no hits, or the absent key term is named as not found

Variants (all read-only on --db, except `notitle`, which needs --alt-db: a copy whose page-level chunks
have an empty FTS title; the lab creates it)
"""
from __future__ import annotations

import argparse
import random
import re
import shutil
import sqlite3
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    import tomli as tomllib

from rzmcp import DB_PATH
from rzmcp.query import STOP, _term

HERE = Path(__file__).parent
DEVICE = "rzg3l"
IDENT_RE = re.compile(r"[A-Za-z0-9]+(?:[_\-.][A-Za-z0-9]+)+")


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------ query building
def terms_plain(q: str) -> list[tuple[str, str]]:
    """Baseline: every alphanumeric run is a term (identifiers fall apart at '_')."""
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q) if t.lower() not in STOP]
    return [(t, _term(t)) for t in dict.fromkeys(toks)]


def terms_phrase(q: str) -> list[tuple[str, str]]:
    """Identifiers joined by _ - . become one FTS phrase: usb_pgood_delay -> "usb pgood delay"."""
    out, rest = [], q
    for m in IDENT_RE.finditer(q):
        parts = re.findall(r"[A-Za-z0-9]+", m.group(0))
        out.append((m.group(0), '"' + " ".join(parts) + '"'))
    rest = IDENT_RE.sub(" ", q)
    out += [(t, _term(t)) for t in re.findall(r"[A-Za-z0-9]+", rest) if t.lower() not in STOP]
    return list(dict.fromkeys(out))


def df(con: sqlite3.Connection, expr: str) -> int:
    return con.execute("SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH ?", (expr,)).fetchone()[0]


def run(con, expr: str, weights, limit: int, seen: set, hits: list, mode: str) -> None:
    sql = ("SELECT c.chunk_id, c.doc_id, c.section_id, c.page, c.locator, "
           f"bm25(chunk_fts, {weights[0]}, {weights[1]}, {weights[2]}) AS rank "
           "FROM chunk_fts JOIN chunk c ON c.chunk_id = chunk_fts.rowid "
           "JOIN document_device dd ON dd.doc_id = c.doc_id AND dd.device = ? "
           "WHERE chunk_fts MATCH ? ORDER BY rank LIMIT 200")
    for r in con.execute(sql, (DEVICE, expr)):
        key = r["section_id"] or ("chunk", r["chunk_id"])
        if key in seen:
            continue
        seen.add(key)
        sec = con.execute("SELECT page_start, page_end FROM section WHERE section_id=?",
                          (r["section_id"],)).fetchone() if r["section_id"] else None
        hits.append({"doc": r["doc_id"], "page": r["page"], "mode": mode,
                     "ps": sec["page_start"] if sec else r["page"], "pe": sec["page_end"] if sec else r["page"]})
        if len(hits) >= limit:
            return


def search(con, q: str, limit: int = 8, weights=(10.0, 2.0, 1.0), phrase: bool = False,
           gate: bool = False) -> tuple[list[dict], list[str]]:
    """Returns (hits, missing): missing = query terms that occur nowhere in the index (gate only)."""
    terms = (terms_phrase if phrase else terms_plain)(q)
    if not terms:
        return [], []
    hits, seen, missing = [], set(), []
    if gate == "detect":             # rank with the given terms; only report identifiers that occur nowhere
        return (search(con, q, limit, weights, phrase, False)[0],
                [raw for raw, e in terms_phrase(q) if df(con, e) == 0])
    if gate:
        missing = [raw for raw, e in terms if df(con, e) == 0]
        terms = [(raw, e) for raw, e in terms if raw not in missing]
        if not terms:
            return [], missing
        # a term that names something specific (identifier / upper-case token) and is absent: do not pad
        if any(IDENT_RE.fullmatch(m) or m.isupper() or len(terms) + len(missing) == 1 for m in missing) \
                and len(missing) >= len(terms):
            return [], missing
    run(con, " AND ".join(e for _, e in terms), weights, limit, seen, hits, "and")
    if len(hits) < limit:
        run(con, " OR ".join(e for _, e in terms), weights, limit, seen, hits, "or")
    return hits, missing


# ------------------------------------------------------------------ query sets
def ascii_query(q: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9_./\-\[\]]+", q))


def load_smoke() -> list[dict]:
    qs = tomllib.load(open(HERE.parent / "smoke_questions.toml", "rb"))["q"]
    out = []
    for q in qs:
        if q["type"] == "negative":
            continue
        truth = [(s["doc_no"].upper(), set(s.get("pages", []))) for s in q["source"]]
        out.append({"id": q["id"], "query": q["question"] if q["question"].isascii() else ascii_query(q["question"]),
                    "truth": truth})
    return out


def load_cases() -> tuple[list[dict], list[dict]]:
    c = tomllib.load(open(HERE / "cases.toml", "rb"))
    pos = [{"id": p["id"], "query": p["query"], "truth": [(p["doc"].upper(), set(p["pages"]))]} for p in c["pos"]]
    return pos, c["neg"]


CAP_RE = re.compile(r"^\s*((?:Table|Figure)\s+[\d.\-]+[A-Za-z]?)\s+([A-Z][^\n]{12,90})$", re.M)


def load_captions(con, n: int, seed: int = 7) -> tuple[list[dict], list[dict]]:
    caps: dict = {}
    for r in con.execute("SELECT c.doc_id, c.page, c.body FROM chunk c JOIN section s USING(section_id) "
                         "WHERE c.page IS NOT NULL AND s.title NOT LIKE '%Contents%' AND s.title NOT LIKE 'List of%'"):
        for m in CAP_RE.finditer(r["body"]):
            text = re.sub(r"\s*\(\d+\s*/\s*\d+\)\s*$", "", m.group(2)).strip()
            if "..." in text or ". ." in text:
                continue
            caps.setdefault((r["doc_id"], text), set()).add(r["page"])
    keys = sorted(caps)
    random.Random(seed).shuffle(keys)
    full, short = [], []
    rnd = random.Random(seed + 1)
    for doc, text in keys[:n]:
        truth = [(doc.upper(), caps[(doc, text)])]
        full.append({"id": f"{doc}:{text[:30]}", "query": text, "truth": truth})
        words = [w for w in dict.fromkeys(re.findall(r"[A-Za-z0-9_]{3,}", text)) if w.lower() not in STOP]
        if len(words) >= 3:
            pick = sorted(rnd.sample(range(len(words)), 3))
            short.append({"id": f"{doc}:{text[:30]}", "query": " ".join(words[i] for i in pick), "truth": truth})
    return full, short


# ------------------------------------------------------------------ scoring
def rank_of(hits: list[dict], truth) -> int | None:
    for i, h in enumerate(hits, 1):
        for doc, pages in truth:
            if h["doc"].upper() != doc:
                continue
            if h["page"] is None:                      # BOM row: the document is the answer when no page is given
                if not pages:
                    return i
                continue
            if h["page"] in pages or (h["pe"] - h["ps"] <= 4 and any(h["ps"] <= p <= h["pe"] for p in pages)):
                return i
    return None


def score(con, qs: list[dict], **kw) -> dict:
    ranks = [rank_of(search(con, q["query"], **kw)[0], q["truth"]) for q in qs]
    n = len(qs)
    return {"n": n, "h1": sum(r == 1 for r in ranks if r), "h3": sum(r <= 3 for r in ranks if r),
            "h8": sum(r <= 8 for r in ranks if r), "mrr": sum(1 / r for r in ranks if r) / n, "ranks": ranks}


def score_neg(con, negs: list[dict], **kw) -> tuple[int, list[str]]:
    ok, detail = 0, []
    for q in negs:
        hits, missing = search(con, q["query"], **kw)
        good = not hits or q["key"] in missing
        ok += good
        detail.append(f"{q['id']}:{'ok' if good else 'padded'}({len(hits)} hits, missing={missing})")
    return ok, detail


def make_alt(src: Path, dst: Path) -> None:
    """Copy of the index in which page-level chunks (no section) do not repeat the document title."""
    shutil.copy(src, dst)
    con = sqlite3.connect(dst)
    con.execute("UPDATE chunk SET title='' WHERE section_id IS NULL AND page IS NOT NULL")
    con.execute("INSERT INTO chunk_fts (chunk_fts) VALUES ('rebuild')")
    con.commit()
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--alt-db", type=Path)
    ap.add_argument("--captions", type=int, default=400)
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    con = connect(a.db)
    alt = None
    if a.alt_db:
        make_alt(a.db, a.alt_db)
        alt = connect(a.alt_db)
    pos, negs = load_cases()
    cap, cap3 = load_captions(con, a.captions)
    sets = {"smoke": load_smoke(), "cases": pos, "caption": cap, "caption3": cap3}
    variants = [("baseline (10,2,1)", con, {}),
                ("weights (5,2,1)", con, {"weights": (5.0, 2.0, 1.0)}),
                ("weights (3,1,1)", con, {"weights": (3.0, 1.0, 1.0)}),
                ("weights (1,1,1)", con, {"weights": (1.0, 1.0, 1.0)}),
                ("phrase", con, {"phrase": True}),
                ("phrase+gate", con, {"phrase": True, "gate": True})]
    if alt:
        variants += [("notitle", alt, {}),
                     ("notitle+detect", alt, {"gate": "detect"}),
                     ("notitle+phrase+gate", alt, {"phrase": True, "gate": True}),
                     ("notitle+phrase+gate (5,2,1)", alt, {"phrase": True, "gate": True, "weights": (5.0, 2.0, 1.0)})]
    print(f"{'variant':<30}" + "".join(f"{name + ' n=' + str(len(qs)):<28}" for name, qs in sets.items()) + "neg handled")
    print(f"{'':<30}" + "".join(f"{'h@1  h@3  h@8  MRR':<28}" for _ in sets))
    for label, c, kw in variants:
        line = f"{label:<30}"
        keep = {}
        for name, qs in sets.items():
            s = score(c, qs, **kw)
            keep[name] = s
            line += f"{s['h1']:<5}{s['h3']:<5}{s['h8']:<5}{s['mrr']:<13.3f}"
        ok, detail = score_neg(c, negs, **kw)
        print(line + f"{ok}/{len(negs)}")
        if a.v:
            print("    cases ranks: " + " ".join(f"{q['id']}={r}" for q, r in zip(pos, keep["cases"]["ranks"])))
            print("    smoke ranks: " + " ".join(f"{q['id']}={r}" for q, r in zip(sets["smoke"], keep["smoke"]["ranks"])))
            print("    neg: " + "; ".join(detail))


if __name__ == "__main__":
    main()
