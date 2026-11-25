# Run: .venv/bin/python eval/test_workflow.py   (uses a throwaway Yocto project in /tmp/wf-tests/prj)
"""Known-answer tests for B1a: poc-new layout vs rzg3l-lowpower-display, prompt stage progression, install-agents."""
import filecmp, os, re, shutil, subprocess, sys
from pathlib import Path

RZ = Path("/home/mpu-epmaj/max/workspace/rz-mcp")
REF = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1/rzg3l-lowpower-display")
PRJ = Path("/tmp/wf-tests/prj")
fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

shutil.rmtree(PRJ, ignore_errors=True)
(PRJ / "meta-renesas").mkdir(parents=True); (PRJ / "build").mkdir()
os.environ["RZ_POC_PROJECT"] = str(PRJ)
py = sys.executable
run = lambda *a: subprocess.run([py, *map(str, a)], capture_output=True, text=True)

# 1. poc-new layout == rzg3l-lowpower-display
r = run(RZ / "workflow/poc-new", "lowpower-display", "--title", "LowPower Display")
check(r.returncode == 0, f"poc-new runs ({r.stderr.strip()[:200]})")
new = PRJ / "rzg3l-lowpower-display"
top = lambda p: sorted(x.name for x in p.iterdir() if x.name not in (".git", "design"))
check(top(new) == top(REF), f"top-level entries {top(new)} == reference {top(REF)}")
g = "docs/RZG3L-LowPower-Display-Guide.md"
check((new / g).exists() and (REF / g).exists(), "guide file name matches the reference")
h2 = lambda p: [l.strip() for l in p.read_text().splitlines() if l.startswith("## ")]
check(h2(new / g) == h2(REF / g), "guide '## ' headings match the reference")
check(filecmp.cmp(new / "tools/md2pdf.py", REF / "tools/md2pdf.py", shallow=False), "md2pdf.py identical")
check((new / ".gitignore").read_text() == (REF / ".gitignore").read_text(), ".gitignore identical")
check(sorted(x.name for x in (new / "layers").iterdir()) == [".gitkeep"], "layers/ starts empty")
check("{" not in (new / "README.md").read_text() and "{" not in (new / g).read_text(), "no unfilled {PLACEHOLDER} in deliverable skeleton")
n = subprocess.run(["git", "-C", str(new), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
check(n == "1", f"one initial commit (got {n})")
check(run(RZ / "workflow/poc-new", "lowpower-display").returncode != 0, "refuses to overwrite an existing repo")
check(run(RZ / "workflow/poc-new", "Bad_Name").returncode != 0, "rejects an invalid name")

# 2. prompt stage progression
sys.path.insert(0, str(RZ / "src"))
from rzmcp import workflow as W
D = new / "design"
def stage(name="lowpower-display"):
    m = re.search(r"^## (.+)$", W.poc_prompt(name).split("当前状态")[0] if False else W.poc_prompt(name).split("\n## 始终遵守")[0], re.M)
    heads = [l for l in W.poc_prompt(name).splitlines() if l.startswith("## ") and "始终遵守" not in l]
    return heads[-1] if heads else ""
def fill(f):
    p = D / f; t = p.read_text().split("\n", 1)[1] if p.exists() else "# written by agent\n"; p.write_text(t + "\n(filled by test)\n")
check("创建 PoC 仓库" in stage("other-poc"), "missing repo -> create step")
check("保存需求原文" in stage(), "fresh repo -> save request")
fill("00-request.md");  check("复述与对齐" in stage(), "after 00 -> restate/align (v1)")
fill("00a-restated.md"); check("深化" in stage(), "after 00a -> sharpen (v2)")
fill("00b-sharpened.md"); s = W.poc_prompt("lowpower-display")
check("两轨独立设计" in s and "两个" in s and "install-agents" in s, "after 00b -> two designers; asks to install agents")
fill("01a-design-v1.md"); s = W.poc_prompt("lowpower-display")
check("两轨独立设计" in s and "一个" in s and "轨道 B" in s and "轨道 A" not in s.split("## 下一步")[1], "after 01a -> only track B")
fill("01b-design-v2.md"); check("比较与融合" in stage(), "after 01b -> merge")
fill("01-design.md"); check("比较与融合" in stage(), "merge needs both 01-design and notes")
fill("01-merge-notes.md"); check("用户对齐" in stage(), "after merge -> user alignment")
fill("02-decisions.md"); check("用户对齐" in stage(), "decisions filled but no tag -> still alignment")
subprocess.run(["git", "-C", str(new), "tag", "design-v1"], check=True)
check("已完成" in stage(), "tag design-v1 -> steps 1-4 done")
check("不合法" in W.poc_prompt("Bad Name"), "invalid name -> error text")
ex = PRJ / "rzg3l-delivered"; (ex / "docs").mkdir(parents=True)
check("不是用 poc-new 创建的" in stage("delivered"), "existing repo without design/ -> stop")

# 3. install-agents
check(run(RZ / "workflow/install-agents", "--check").returncode == 1, "--check fails before install")
check(run(RZ / "workflow/install-agents").returncode == 0, "install runs")
check(run(RZ / "workflow/install-agents", "--check").returncode == 0, "--check passes after install")
check(W.agents_current(PRJ), "prompt sees agents as current")
check("install-agents" not in W.poc_prompt("other-poc"), "no install hint once installed (create step)")

# 4. agent frontmatter
for f in sorted((RZ / "workflow/agents").glob("*.md")):
    t = f.read_text(); fm = t.split("---")[1]
    keys = dict(l.split(":", 1) for l in fm.strip().splitlines())
    check(t.startswith("---\n") and keys.get("name", "").strip() == f.stem and "description" in keys
          and keys.get("disallowedTools", "").strip() == "Agent", f"{f.name}: frontmatter name/description/disallowedTools")

print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
