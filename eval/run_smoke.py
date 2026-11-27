"""LLM-free smoke evaluation of the query layer against eval/smoke_questions.toml.

    PYTHONPATH=src python3 eval/run_smoke.py [-v]

Two checks per question:
  retrieval  : search_docs(<question text>, limit=8) — is any cited (doc, page|section) among the hits?
               The raw question is a pessimistic query; a real agent extracts better keywords.
  structured : for questions whose sources lie in a structured module, lookup_register(<register names
               mentioned in the question/key facts>) — do all key_facts / derived_facts appear in the output?
This measures the data and the tools, not an agent's tool choice; the end-to-end test is a person using
Claude Code against the server with the same questions.
"""
import re
import sys
import tomllib
from pathlib import Path

from rzmcp import query as Q

HERE = Path(__file__).parent
HIT_RE = re.compile(r"^\[(\d+)\] (\S+) Rev \S+ \[\w+\] (?:§(\S+) )?(.*?) — hit at (?:p\.(\d+)|BOM row (\d+)|[^\n(]+)", re.M)


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"\b([0-9A-F][0-9A-F_]*)h\b", r"H'\1", s)      # 0_1101_0B10h -> H'0_1101_0B10
    s = s.lower()
    s = re.sub(r"[\s_]", "", s)
    s = s.replace("0x", "h'")
    s = re.sub(r"h'0+(?=[0-9a-f])", "h'", s)
    return s


def ascii_query(q: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9_./\-\[\]]+", q))


def structured_modules() -> dict[str, tuple[str, str]]:
    return {r["section_number"]: (r["name"], r["doc_id"]) for r in Q.db().execute("SELECT * FROM module")}


def main(verbose: bool) -> None:
    qs = tomllib.load(open(HERE / "smoke_questions.toml", "rb"))["q"]
    mods = structured_modules()
    reg_names = {r[0] for r in Q.db().execute("SELECT name FROM register UNION SELECT template FROM register_def")}
    n_ret = n_ret_hit = n_str = n_str_ok = 0
    rows = []
    for q in qs:
        query = q["question"] if q["question"].isascii() else ascii_query(q["question"])
        out = Q.search_docs(query, limit=8)
        hits = [(m.group(2), m.group(3) or (m.group(4).split(" ")[0] if re.match(r"\d", m.group(4)) else None),
                 int(m.group(5)) if m.group(5) else None, m.group(6)) for m in HIT_RE.finditer(out)]
        want = [(s["doc_no"], s.get("section", ""), set(s.get("pages", []))) for s in q["source"]]
        rank = None
        for i, (doc, sec, page, bomrow) in enumerate(hits, 1):
            for wdoc, wsec, wpages in want:
                if doc.upper() == wdoc.upper() and ((page and page in wpages) or (sec and wsec and sec == wsec)
                                                    or (bomrow and not wpages)):
                    rank = rank or i
        if q["type"] != "negative":
            n_ret += 1
            n_ret_hit += rank is not None
        # structured check
        in_struct = any(any(s.get("section", "").startswith(num + ".") or s.get("section", "") == num for num in mods)
                        and s["doc_no"] == "R01UH1153EJ0110" for s in q["source"])
        sres = ""
        if in_struct and q["type"] in ("register_lookup", "bitfield"):
            text = q["question"] + " " + " ".join(q.get("key_facts", []))
            names = [n for n in dict.fromkeys(re.findall(r"[A-Z][A-Z0-9_]{1,}(?:_n)?", text))
                     if n in reg_names or any(r.startswith(n + "_") for r in reg_names)]
            # like an agent: also take the top candidates of search_registers(question)
            sr = Q.search_registers(query, limit=3)
            names += [m for m in re.findall(r"^- (\S+)", sr, re.M) if m not in names]
            s_out = "\n".join(Q.lookup_register(n) for n in names)
            t_out = "\n".join(Q.get_section(src["section"], doc_id=src["doc_no"], max_chars=10000)
                              for src in q["source"] if src.get("section"))
            facts = q.get("key_facts", []) + q.get("derived_facts", [])
            where = {f: "S" if norm(f) in norm(s_out) else "T" if norm(f) in norm(t_out) else "M" for f in facts}
            n_str += 1
            n_str_ok += all(v != "M" for v in where.values())
            n_fact_s = sum(v == "S" for v in where.values())
            sres = f"S {n_fact_s}/{len(facts)} facts in lookup_register" + \
                   (f"; +{sum(v == 'T' for v in where.values())} only in section text" if any(v == "T" for v in where.values()) else "") + \
                   (f"; MISSING {[f for f, v in where.items() if v == 'M']}" if any(v == "M" for v in where.values()) else "")
            if verbose:
                print(f"--- {q['id']} names={names} " + " ".join(f"{k!r}:{v}" for k, v in where.items()))
        rows.append((q["id"], q["type"], rank, sres, query[:60]))
    print(f"{'id':<8} {'type':<16} {'ret@8':<6} structured")
    for i, t, r, s, qq in rows:
        print(f"{i:<8} {t:<16} {str(r) if r else ('n/a' if t == 'negative' else 'MISS'):<6} {s}")
    print(f"\nretrieval hit@8: {n_ret_hit}/{n_ret}   register questions fully answerable from tool output: {n_str_ok}/{n_str}")


if __name__ == "__main__":
    main("-v" in sys.argv)
