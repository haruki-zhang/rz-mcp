#!/usr/bin/env python3
"""Prototype 3 measured: the queries the designer ran on the Start-up Guide (pdftotext + grep, evk-mac 2026-11-26)
and the pages F-20 of the merge notes cites, against a database with the guide added.

    PYTHONPATH=src python3 eval/gap/sug_eval.py <db>
"""
import re
import sys
from pathlib import Path

from rzmcp import query as Q

Q.set_db(Path(sys.argv[1]))
SUG = "R01US0858EJ0100"
# the designer's grep -i terms (ethaddr|MAC address|No valid|env default|saveenv|SW_SD0|SYS.1|SYS1), as queries
AGENT = ["No valid MAC address", "ethaddr", "env default saveenv", "SW_SD0_DEV_SEL", "SYS1 eMMC microSD"]
# F-20: official eSD / eMMC boot logs (p.26, 27, 39, 42), eMMC and the SOM slot exclusive (p.37, 41), SYS_LSI_MODE
CITED = {26, 27, 37, 39, 41, 42}
HIT = re.compile(r"^\[(\d+)\] (\S+) Rev \S+ \[\w+\] .*? — hit at p\.(\d+)", re.M)
n_any = 0
pages = set()
for q in AGENT + ["U-Boot boot log eSD", "U-Boot boot log eMMC", "SYS_LSI_MODE"]:
    hs = [(int(r), d, int(p)) for r, d, p in HIT.findall(Q.search_docs(q, limit=8))]
    sug = [(r, p) for r, d, p in hs if d == SUG]
    cited = [(r, p) for r, p in sug if p in CITED]
    pages |= {p for _, p in sug}
    n_any += bool(sug)
    print(f"  {q:28} guide hits (rank, page): {sug[:5]}  on F-20 pages: {cited[:3]}")
print(f"queries answered from the guide: {n_any}; F-20 pages reached: {sorted(pages & CITED)} of {sorted(CITED)}")
