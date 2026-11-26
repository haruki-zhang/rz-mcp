#!/usr/bin/env python3
"""Where did the agents leave the MCP server? Classify the calls extracted by extract.py.

    python3 eval/gap/classify.py <calls.jsonl> [--examples N] [--json out.json]

MCP calls: empty results ("misses") and what the agent did next (another MCP call, a Bash call, nothing).
bsp_source: how often one file was read in pieces. Bash calls: what they were for. A Bash call that reads
something an MCP tool could have answered is a "detour" (the categories marked * below); running prototypes,
reading or writing the PoC's own files, builds and board work are not.
"""
import argparse
import collections
import json
import re

MISS = re.compile(r"No matches for|\(no hits\)|: 0 hit\(s\)|not found at this revision|No register named|"
                  r"No section|not found in|unknown (doc|component)")

# (category, is a detour, pattern on the command); first match wins. Labels were checked by reading every command
# of the evk-mac run (2026-11-26); OPS first, so that writing, building and board work never count as a detour.
OPS = r"evk-flash|evk-verify|evk-watch|poc-build|poc-use |poc-package|poc-new|install-agents|yocto-project|git (add|commit)|" \
      r"\bcp (-L )?\S+ \S*rzg3l-evk-mac|cat > |PYEOF|git clone -q --shared|md2pdf|/tmp/claude-evk"
BASH = [
    ("ops: build, board, writing the PoC's files", False, OPS),
    ("PDF not in the index *", True, r"pdftotext|pdfinfo|pdftohtml|mutool|import fitz|import pypdf"),
    ("toolchain for host prototypes (cross gcc, sysroot, qemu, dtc) *", True, r"sysroots-components|hosttools|qemu-aarch64|recipe-sysroot|which (gcc|clang|cc)|ls /usr/bin/ \| grep"),
    ("other components' official source (git mirrors) *", True, r"git2/(?!github\.com\.renesas-rz\.(rz_linux-cip|renesas-u-boot-cip|rzg_trusted-firmware-a))|--git-dir=\S*downloads|downloads \| grep|downloads/git2 \|"),
    ("past PoCs and past board logs *", True, r"rzg3l-bsp-v1/?(\s|&&|;|$)|rzg3l-bsp-v1/(rzg3l-|MultiCore-Display|logs|artifacts|meta-rzg3l-combo)|rzg3l-lowpower-display|fast-boot-seamless|MultiCore-Display"),
    ("BSP layer metadata: recipes, machine / distro conf, wks *", True, r"meta-renesas|poky/meta|\bpoky\b.*recipes-|meta-openembedded/|meta-arm/|bitbake-layers|bitbake -e"),
    ("build results: work dirs, .config, compile logs *", True, r"build/tmp\S*/work/|log\.do_|/\.config\b"),
    ("build configuration and state *", True, r"build/conf|poc-use\.json|DL_DIR|SSTATE_DIR|pgrep -a bitbake"),
    ("prototypes and tests (python, gcc)", False, r"python3? |gcc|\bcc\b|make\b|proto-|test_"),
    ("the PoC's own files and git", False, r"/design|rzg3l-evk-mac|git (diff|log|status|tag|show)|\bgit -C"),
]


# Commands the patterns put in the wrong place, found by reading them (evk-mac, 2026-11-26): (substring, category)
OVERRIDES = [
    ("grep -rIl 'R01UH1152", "ops: build, board, writing the PoC's files"),      # checks the delivery for confidential refs
    ("cd /tmp/claude-uboot", "ops: build, board, writing the PoC's files"),       # working copy for the U-Boot patches
    ("ls proto-merge", "prototypes and tests (python, gcc)"),
    ("sed -i 's/方案只公开", "the PoC's own files and git"),                       # edits its own design
    ("ls -l /dev/serial/by-id", "ops: build, board, writing the PoC's files"),    # board connection check
    ("ls -la /home/mpu-epmaj/max/workspace/rzg3l-bsp/rzg3l-evk-mac/ /home", "other (listing, environment)"),
    ("ls -la /home/mpu-epmaj/max/workspace/rzg3l-bsp/rzg3l-evk-mac /home", "other (listing, environment)"),
    ("packagegroup-rz-vlp-tools.bb | gr", "BSP layer metadata: recipes, machine / distro conf, wks *"),
]


def bash_cat(cmd: str) -> tuple[str, bool]:
    for sub, name in OVERRIDES:
        if sub in cmd:
            return name, name.endswith("*")
    for name, detour, rx in BASH:
        if re.search(rx, cmd):
            return name, detour
    return "other (listing, environment)", False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("calls")
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.calls, encoding="utf-8")]
    by_agent = collections.defaultdict(list)
    for r in rows:
        by_agent[r["agent"]].append(r)
    out = {"mcp": {}, "misses": [], "bash": {}, "paging": {}, "per_agent": {}}

    # ---- MCP calls, misses and what followed
    mcp = collections.Counter(r["tool"] for r in rows if r["tool"].startswith("mcp:"))
    for agent, rs in by_agent.items():
        for k, r in enumerate(rs):
            if r["tool"].startswith("mcp:") and MISS.search(r.get("result", "")):
                nxt = next((x for x in rs[k + 1:k + 4] if x["tool"] not in ("ToolSearch",)), None)
                follow = "none" if not nxt else "MCP again" if nxt["tool"].startswith("mcp:") else nxt["tool"]
                out["misses"].append({"agent": agent, "tool": r["tool"], "input": r["input"],
                                      "result": r.get("result", "")[:160], "next": follow,
                                      "next_input": (nxt or {}).get("input")})
    out["mcp"] = {t: {"calls": n, "misses": sum(1 for m in out["misses"] if m["tool"] == t)} for t, n in mcp.most_common()}

    # ---- bsp_source: one file read in pieces
    files = collections.Counter((r["input"].get("component"), r["input"].get("path")) for r in rows if r["tool"] == "mcp:bsp_source")
    out["paging"] = {"calls": sum(files.values()), "distinct_paths": len(files),
                     "paths_read_3_or_more_times": sorted(((f"{c}:{p}", n) for (c, p), n in files.items() if n >= 3), key=lambda x: -x[1])}

    # ---- Bash
    cats = collections.defaultdict(list)
    for r in rows:
        if r["tool"] == "Bash":
            name, detour = bash_cat(r["input"].get("command", ""))
            cats[(name, detour)].append(r)
    for (name, detour), rs in sorted(cats.items(), key=lambda kv: -len(kv[1])):
        per = collections.Counter(x["agent"].split(":")[0] for x in rs)
        out["bash"][name] = {"detour": detour, "calls": len(rs), "by_agent": dict(per),
                             "examples": [x["input"]["command"][:220].replace("\n", " ; ") for x in rs[:a.examples]]}
    for agent, rs in by_agent.items():
        b = [x for x in rs if x["tool"] == "Bash"]
        out["per_agent"][agent] = {"mcp": sum(x["tool"].startswith("mcp:") for x in rs), "bash": len(b),
                                   "detours": sum(bash_cat(x["input"].get("command", ""))[1] for x in b)}

    if a.json:
        json.dump(out, open(a.json, "w"), ensure_ascii=False, indent=1)
    print("MCP calls (misses):", ", ".join(f"{t[4:]} {v['calls']} ({v['misses']})" for t, v in out["mcp"].items()))
    print("\nper agent:", json.dumps(out["per_agent"], ensure_ascii=False, indent=1))
    print(f"\nbsp_source: {out['paging']['calls']} calls on {out['paging']['distinct_paths']} paths; read >=3 times:",
          out["paging"]["paths_read_3_or_more_times"][:8])
    print("\nBash:")
    for name, v in out["bash"].items():
        print(f"  {v['calls']:3}  {'detour ' if v['detour'] else '       '}{name}  {v['by_agent']}")
        for e in v["examples"]:
            print(f"         {e}")
    print("\nmisses and what followed:")
    for m in out["misses"]:
        print(f"  {m['agent'][:24]:24} {m['tool'][4:]:16} {json.dumps(m['input'], ensure_ascii=False)[:90]}  -> {m['next']}")


if __name__ == "__main__":
    main()
