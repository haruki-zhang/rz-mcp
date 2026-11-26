#!/usr/bin/env python3
"""Replay the agents' past-PoC detours against prototype 2 (eval/gap/proto/past.py).

    python3 eval/gap/replay_past.py <calls.jsonl>

Same method as replay_layers.py: recover what each detour read; a target is servable if it lies in a registered
past PoC or the shared project's logs; for files, compare prototype content with what was on disk.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "proto"))
import classify as C        # noqa: E402
import past as P            # noqa: E402
from replay_layers import targets  # noqa: E402


def where(p: Path) -> str | None:
    try:
        rel = p.resolve().relative_to(P.SHARED)
    except ValueError:
        return None
    return str(rel) if rel.parts and rel.parts[0] in P.SOURCES else None


def main() -> None:
    rows = [json.loads(l) for l in open(sys.argv[1])]
    det = [r for r in rows if r["tool"] == "Bash" and C.bash_cat(r["input"].get("command", ""))[0].startswith("past PoCs")]
    full = part = none = files = same = gitprov = 0
    for r in det:
        paths, _ = targets(r["input"]["command"])
        mine = [p for p in paths if where(p)]
        other = [p for p in paths if not where(p) and "rzg3l-evk-mac" not in str(p) and "/design" not in str(p)
                 and str(p).rstrip("/") != str(P.SHARED)]
        v = "all" if mine and not other else "part" if mine else "none"
        full += v == "all"
        part += v == "part"
        none += v == "none"
        for p in mine:
            if p.is_file():
                files += 1
                got = P.past_source(where(p))
                same += got is not None and got.splitlines() == p.read_text(errors="replace").splitlines()
                gitprov += P.resolve(where(p))[2] is not None
        if v != "all":
            print(f"  {v:4} {r['agent'][:12]:12} {r['input']['command'][:110]}\n        other targets: {[str(o) for o in other][:3]}")
    print(f"detours replayed: {len(det)}; every target servable by prototype 2: {full}, some: {part}, none: {none}")
    print(f"files read: {files}; identical through the prototype: {same}; with a git commit as provenance: {gitprov}")


if __name__ == "__main__":
    main()
