#!/usr/bin/env python3
"""Retrieval baseline from a real PoC run (no hand-made questions).

    PYTHONPATH=src python3 eval/gap/retrieval.py <facts.json> <calls.jsonl> [--db data/rz.db]

A. Verified facts with document pages (facts.py): the query is the fact's own ASCII words (register and signal
   names, terms; numbers dropped), made by a fixed rule; a hit is a result on a cited page of the cited document.
   Facts citing documents that are not indexed count as coverage misses.
B. The agents' own search_docs calls: the target is what the same agent opened next (read_pages / get_section
   within its next 3 calls); "surfaced" = that document and page were in the result it got. Re-run on the
   current index as well.
"""
import argparse
import json
import re
from pathlib import Path
import sys

HIT = re.compile(r"^\[(\d+)\] (\S+) Rev \S+ \[\w+\] .*? — hit at p\.(\d+) \(section pp?\.(\d+)(?:-(\d+))?\)", re.M)
STOP = {"the", "and", "of", "in", "to", "is", "a", "for", "on", "at", "by", "or", "as", "be", "rev", "table", "figure",
        "page", "section", "doc", "pdf", "rz", "g3l", "rzg3l"}


def hits(text: str) -> list[tuple[int, str, int, int, int]]:
    return [(int(r), d, int(p), int(a), int(b or a)) for r, d, p, a, b in HIT.findall(text)]


def match(h, cites) -> bool:
    _, doc, p, a, b = h
    return any(c["doc"] == doc and (c["first"] <= p <= c["last"] or (a <= c["first"] <= b)) for c in cites)


def query_of(fact: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]*[A-Za-z0-9]", fact)
    seen, out = set(), []
    for w in words:
        if w.lower() in STOP or w.lower() in seen:
            continue
        seen.add(w.lower())
        out.append(w)
    return " ".join(out[:10])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("facts")
    ap.add_argument("calls")
    ap.add_argument("--db")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    from rzmcp import query as Q
    if a.db:
        Q.set_db(Path(a.db))
    indexed = {r[0] for r in Q.db().execute("SELECT DISTINCT doc_id FROM chunk")}

    # ---- A
    facts = [f for f in json.load(open(a.facts)) if f["pages"]]
    rows, cover_miss = [], 0
    for f in facts:
        cites = [c for c in f["pages"] if c["doc"] in indexed]
        if not cites:
            cover_miss += 1
            rows.append((f["id"], None, "document not indexed: " + ", ".join(sorted({c['doc'] for c in f['pages']}))))
            continue
        q = query_of(f["fact"])
        res = Q.search_docs(q, limit=8)
        hs = hits(res)
        rank = next((h[0] for h in hs if match(h, cites)), None)
        rows.append((f["id"], rank, q))
    ranked = [r for r in rows if r[1] is not False]
    n_idx = len(facts) - cover_miss
    at = lambda k: sum(1 for _, r, _ in rows if r and r <= k)
    print(f"A. facts with document pages: {len(facts)}; cite an un-indexed document only: {cover_miss}")
    print(f"   on indexed documents: {n_idx}; hit@1 {at(1)}, hit@3 {at(3)}, hit@8 {at(8)}")
    if not a.quiet:
        for fid, r, q in rows:
            print(f"     {fid} {'miss' if r is None else 'rank ' + str(r):8} {q[:100]}")

    # ---- B
    calls = [json.loads(l) for l in open(a.calls)]
    by = {}
    for c in calls:
        by.setdefault(c["agent"], []).append(c)
    total = surf = now = with_target = 0
    detail = []
    for agent, cs in by.items():
        for k, c in enumerate(cs):
            if c["tool"] != "mcp:search_docs":
                continue
            total += 1
            tgt = None
            for x in cs[k + 1:k + 4]:
                if x["tool"] == "mcp:read_pages":
                    s = x["input"].get("start_page")
                    e = x["input"].get("end_page") or s
                    tgt = [{"doc": x["input"]["doc_id"], "first": s, "last": e}]
                    break
                if x["tool"] == "mcp:get_section":
                    m = re.search(r"\[p\.(\d+)\]", x.get("result", ""))
                    d = x["input"].get("doc_id") or "R01UH1153EJ0110"
                    if m:
                        tgt = [{"doc": d, "first": int(m.group(1)), "last": int(m.group(1)) + 3}]
                    break
            if not tgt:
                continue
            with_target += 1
            got = any(match(h, tgt) for h in hits(c.get("result", "")))
            rerun = any(match(h, tgt) for h in hits(Q.search_docs(c["input"]["query"], device=c["input"].get("device", "rzg3l"),
                                                                     doc_type=c["input"].get("doc_type"), doc_id=c["input"].get("doc_id"),
                                                                     limit=c["input"].get("limit", 8))))
            surf += got
            now += rerun
            detail.append((agent[:20], c["input"]["query"][:60], tgt[0]["doc"], tgt[0]["first"], got, rerun))
    print(f"\nB. agents' search_docs calls: {total}; followed by opening a page: {with_target}; "
          f"that page was in the result: {surf} (re-run on the current index: {now})")
    if not a.quiet:
        for d in detail:
            print(f"     {d[0]:20} {d[1]:60} -> {d[2]} p.{d[3]}  {'surfaced' if d[4] else 'NOT in result'}")


if __name__ == "__main__":
    main()
