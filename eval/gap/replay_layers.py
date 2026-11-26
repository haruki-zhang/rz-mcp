#!/usr/bin/env python3
"""Replay the agents' layer-metadata and other-source detours against prototype 1 (eval/gap/proto/layers.py).

    PYTHONPATH=src python3 eval/gap/replay_layers.py <calls.jsonl>

For each Bash detour of those two categories, the files and directories it read are recovered from the command
(cd, VAR=... assignments, globs). A target is servable if it lies in a BSP layer of a Yocto project (layer_source
/ layer_grep) or is read from a component's git mirror that its recipe names (recipe_source). For every servable
file the prototype's content is compared with the file on disk: equal means the agent would have seen the same
text, now with its provenance (layer@rev:path).
"""
import glob
import json
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "proto"))
import classify as C  # noqa: E402
import layers as L    # noqa: E402

PROJECTS = [Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp"), Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1")]
READERS = {"cat", "sed", "head", "tail", "grep", "ls", "find", "wc", "awk"}


def segments(cmd: str) -> list[list[str]]:
    """Shell words grouped into simple commands, split at ; && || | outside quotes."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    segs, cur = [], []
    try:
        for tok in lex:
            if tok and set(tok) <= set(";&|"):
                segs.append(cur)
                cur = []
            else:
                cur.append(tok)
    except ValueError:                                   # unbalanced quotes (heredocs): fall back to words
        return [cmd.split()]
    return segs + [cur]


def targets(cmd: str) -> tuple[list[Path], list[tuple[str, str, str]]]:
    """(paths read, git-mirror reads (mirror dir, rev, path))."""
    env, cwd, paths, gits = {}, None, [], []
    for toks in segments(cmd):
        toks = [t for t in toks if ">" not in t and not t.startswith("/dev/")]
        if len(toks) == 1 and re.match(r"^[A-Z]\w*=", toks[0]):
            k, v = toks[0].split("=", 1)
            env[k] = v
            continue
        toks = [re.sub(r"\$\{?([A-Z]\w*)\}?", lambda m: env.get(m.group(1), m.group(0)), t) for t in toks]
        if not toks:
            continue
        if toks[0] == "cd" and len(toks) > 1:
            cwd = Path(toks[1])
            continue
        if toks[0] == "git" and any(t.startswith("--git-dir=") for t in toks):
            mirror = Path(next(t for t in toks if t.startswith("--git-dir=")).split("=", 1)[1]).name
            spec = next((t for t in toks if re.match(r"^[0-9a-f]{40}:", t)), "")
            gits.append((mirror, spec[:40], spec[41:]))
            continue
        if toks[0] not in READERS:
            continue
        args = [t for t in toks[1:] if not t.startswith("-")]
        if toks[0] in ("grep", "sed", "awk") and args and "-e" not in toks:
            args = args[1:]                                  # the pattern or script
        for a in args:
            p = Path(a) if a.startswith("/") else (cwd / a if cwd else None)
            if not p:
                continue
            found = glob.glob(str(p))
            if not found and "/" not in a and not re.search(r"\.(bb|bbappend|inc|conf|wks|cfg|bbclass|md|log)$", a):
                continue                                     # a word, not a path
            for q in (found or [str(p)]):
                paths.append(Path(q))
        if toks[0] in ("grep", "find") and cwd and not any(t for t in args if not t.startswith("-")):
            paths.append(cwd)                                # grep -r PATTERN with no path / find with none: the cwd
    return paths, gits


def where(p: Path) -> tuple[Path, str] | None:
    for prj in PROJECTS:
        try:
            rel = p.resolve().relative_to(prj)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in L.LAYERS:
            return prj, str(rel)
    return None


def main() -> None:
    rows = [json.loads(l) for l in open(sys.argv[1])]
    cats = ("BSP layer metadata: recipes, machine / distro conf, wks *", "other components' official source (git mirrors) *")
    det = [r for r in rows if r["tool"] == "Bash" and C.bash_cat(r["input"].get("command", ""))[0] in cats]
    full = part = none = files = same = 0
    out = []
    for r in det:
        paths, gits = targets(r["input"]["command"])
        bsp = [p for p in paths if where(p)]
        other = [p for p in paths if not where(p) and "/design" not in str(p) and "rzg3l-evk-mac" not in str(p)
                 and "/downloads" not in str(p)]
        git_ok = [g for g in gits if any((L.recipe_mirror(prj, "systemd") or ("", ""))[0] == g[0] for prj in PROJECTS)]
        servable = len(bsp) + len(git_ok)
        unserv = len(other) + len(gits) - len(git_ok)
        verdict = "all" if servable and not unserv else "part" if servable else "none"
        full += verdict == "all"
        part += verdict == "part"
        none += verdict == "none"
        for p in bsp:
            prj, rel = where(p)
            if p.is_file():
                files += 1
                got = L.layer_source(prj, rel, 1, 10 ** 6).split("\n", 1)[1]
                same += got == "\n".join(p.read_text(errors="replace").splitlines())
        out.append((verdict, r["agent"][:12], r["input"]["command"][:120].replace("\n", " ; "),
                    [str(p) for p in other][:2], [g[0] for g in gits if g not in git_ok]))
    print(f"detours replayed: {len(det)}; every target servable by prototype 1: {full}, some: {part}, none: {none}")
    print(f"files read that lie in BSP layers: {files}; prototype content identical to what the agent read: {same}")
    for v, a, c, o, g in out:
        if v != "all":
            print(f"  {v:4} {a:12} {c}\n        not servable: {o} {g}")


if __name__ == "__main__":
    main()
