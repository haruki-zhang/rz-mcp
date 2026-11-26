#!/usr/bin/env python3
"""The verified facts of a PoC's merge notes (01-merge-notes.md section 1) as a known-answer set.

    python3 eval/gap/facts.py <01-merge-notes.md> [--json out.json]

Each row "| F-nn <fact> | <from> | <result> | <sources> |" gives the fact and where it was verified. Sources are
split by kind: document pages (doc id + p.N; rows without a doc id cite the device's hardware manual), official
source (kernel / u-boot / tf-a:path), BSP layer metadata (meta-renesas / poky:path), MCP tool output, other.
The result column (通过 / 不通过 / 无法核实) is the merger's verdict, made by re-reading the sources.
"""
import argparse
import json
import re
from pathlib import Path

DEFAULT_DOC = "R01UH1153EJ0110"           # the RZ/G3L hardware manual (public edition)
DOC_ID = r"(R01[A-Z]{2}\d{4}EJ\d{4}|D0\d{5}_\d{2}(?:_S\d{2})?_V\d{4})"
PAGE = r"p\.(\d+)(?:\s*[–-]\s*(\d+))?"


def parse(md: str) -> list[dict]:
    sec = md.split("\n## 1.", 1)[1].split("\n## 2.", 1)[0]
    out = []
    for line in sec.splitlines():
        m = re.match(r"\|\s*(F-\d+)\s+(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$", line)
        if not m:
            continue
        fid, fact, frm, verdict, src = m.groups()
        verdict = "通过" if "通过" in verdict and "不通过" not in verdict else "不通过" if "不通过" in verdict else \
            "无法核实" if "无法核实" in verdict else verdict
        pages, doc = [], None
        for tok in re.finditer(DOC_ID + "|" + PAGE, fact + " " + src):
            if tok.group(1):
                doc = tok.group(1)
            elif tok.group(2):
                a = int(tok.group(2))
                b = int(tok.group(3)) if tok.group(3) and int(tok.group(3)) >= a else a
                pages.append({"doc": doc or DEFAULT_DOC, "first": a, "last": b})
        kinds = set()
        if pages:
            kinds.add("doc pages")
        if re.search(r"\b(kernel|u-boot|tf-a):[\w./-]+", src) or re.search(r"\b(net|drivers|include|cmd|env|lib|common|board|arch|plat)/[\w./-]+\.\w+:\d", src):
            kinds.add("official source (kernel / U-Boot / TF-A)")
        if re.search(r"\b(meta-renesas|poky|meta-openembedded|meta-arm):[\w./-]+", src):
            kinds.add("BSP layer metadata")
        if re.search(r"systemd|busybox", src) and re.search(r":\d", src):
            kinds.add("other components' source")
        if re.search(r"`(get_bsp_release|check_build_env|patch_stack|bsp_grep|list_documents|search_docs)", src):
            kinds.add("MCP tool output")
        if re.search(r"过去 PoC|\$PAST|logs/|fast-boot|puccini|\.md` 第|_common\.mk", src):
            kinds.add("past PoCs / past board logs")
        if re.search(r"D0\d{5}_\d{2}_V\d{4}.*BOM|BOM", src):
            kinds.add("BOM / schematic")
        if re.search(r"推断", src) and not kinds:
            kinds.add("inference")
        if re.search(r"proto-|run_\w+\.out|复算|重跑", src):
            kinds.add("prototype / computation")
        if not kinds:
            kinds.add("other")
        out.append({"id": fid, "fact": fact, "verdict": verdict, "sources": src, "pages": pages, "kinds": sorted(kinds)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("notes", type=Path)
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = parse(a.notes.read_text(encoding="utf-8"))
    if a.json:
        json.dump(rows, open(a.json, "w"), ensure_ascii=False, indent=1)
    from collections import Counter
    print(f"{len(rows)} facts; verdicts:", dict(Counter(r["verdict"] for r in rows)))
    print("source kinds (a fact can have several):", dict(Counter(k for r in rows for k in r["kinds"])))
    print("documents cited:", dict(Counter(p["doc"] for r in rows for p in r["pages"])))
    for r in rows:
        if r["verdict"] != "通过":
            print(f"  {r['id']} {r['verdict']}: {r['fact'][:70]}  <- {r['sources'][:110]}")


if __name__ == "__main__":
    main()
