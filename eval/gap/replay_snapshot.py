#!/usr/bin/env python3
"""Acceptance replay for the source-snapshot tools (rzmcp.bsp bsp_source / bsp_grep with layer components): the
same 40 layer-metadata / other-source detours as replay_layers.py, but every BSP-layer file is fetched through
bsp_source(<layer component>, <path>, project=...) and compared line by line with the file on disk.

    PYTHONPATH=src python3 eval/gap/replay_snapshot.py <calls.jsonl>
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import classify as C                        # noqa: E402
from replay_layers import targets, where     # noqa: E402
from rzmcp import bsp as B                   # noqa: E402

LINE = re.compile(r"^\s*(\d+)  (.*)$")


def comp_and_path(rel: str) -> tuple[str, str]:
    parts = rel.split("/")
    if parts[0] == "meta-rz-features" and len(parts) > 2:
        return parts[1], "/".join(parts[2:])          # meta-rz-features/meta-rz-graphics/x -> meta-rz-graphics, x
    return parts[0], "/".join(parts[1:])


def fetch(comp: str, path: str, project: Path) -> list[str] | None:
    out, start = [], 1
    while True:
        try:
            t = B.bsp_source(comp, path, start, 2000, project=str(project))
        except Exception as e:                        # unknown component, path outside the view ...
            return None
        body = [LINE.match(l) for l in t.splitlines()[1:]]
        got = [m.group(2) for m in body if m]
        if not got:
            return out if out else (None if "not found" in t or "no such" in t.lower() else [])
        out += got
        m = re.search(r"start_line=(\d+)", t)
        if not m:
            return out
        start = int(m.group(1))


def main() -> None:
    rows = [json.loads(l) for l in open(sys.argv[1])]
    cats = ("BSP layer metadata: recipes, machine / distro conf, wks *", "other components' official source (git mirrors) *")
    det = [r for r in rows if r["tool"] == "Bash" and C.bash_cat(r["input"].get("command", ""))[0] in cats]
    full = part = none = files = same = missing = 0
    gaps = []
    cache = {}
    for r in det:
        paths, gits = targets(r["input"]["command"])
        ok = bad = 0
        for p in paths:
            w = where(p)
            if not w:
                continue
            prj, rel = w
            if not p.is_file():
                ok += 1                                # a directory: bsp_grep / a listing serves it
                continue
            files += 1
            comp, path = comp_and_path(rel)
            key = (str(prj), comp, path)
            if key not in cache:
                cache[key] = fetch(comp, path, prj)
            got = cache[key]
            if got is None:
                missing += 1
                bad += 1
                gaps.append(f"{prj.name}: {comp}:{path}")
                continue
            ok += 1
            same += got == p.read_text(errors="replace").splitlines()
        bad += len(gits)                               # other components' source: not served by the snapshot
        if gits:
            gaps += [f"git mirror {g[0]}" for g in gits]
        v = "all" if ok and not bad else "part" if ok else "none"
        full += v == "all"
        part += v == "part"
        none += v == "none"
    print(f"detours replayed: {len(det)}; every target served by the snapshot tools: {full}, some: {part}, none: {none}")
    print(f"layer files read: {files}; served: {files - missing}; identical to disk: {same}")
    from collections import Counter
    for g, n in Counter(gaps).most_common(12):
        print(f"  not served ({n}): {g}")


if __name__ == "__main__":
    main()
