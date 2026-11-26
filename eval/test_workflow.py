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
check(set((REF / ".gitignore").read_text().splitlines()) <= set((new / ".gitignore").read_text().splitlines())
      and "dist/" in (new / ".gitignore").read_text(), ".gitignore = the reference's lines + dist/")
check(sorted(x.name for x in (new / "layers").iterdir()) == [".gitkeep"], "layers/ starts empty")
check("{" not in (new / "README.md").read_text() and "{" not in (new / g).read_text(), "no unfilled {PLACEHOLDER} in deliverable skeleton")
n = subprocess.run(["git", "-C", str(new), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
check(n == "1", f"one initial commit (got {n})")
check(run(RZ / "workflow/poc-new", "lowpower-display").returncode != 0, "refuses to overwrite an existing repo")
check(run(RZ / "workflow/poc-new", "Bad_Name").returncode != 0, "rejects an invalid name")

# 2. prompt stage progression
sys.path.insert(0, str(RZ / "src"))
os.environ["RZ_POC_REGISTRY"] = str(PRJ.parent / "poc-projects.json")
from rzmcp import workflow as W
# The throwaway project is not a real Yocto project. Step 0 (the project itself) is tested in test_yocto_project.py;
# here the project counts as usable so that only the PoC's own stages are exercised.
W.Y.assess = lambda p, *a, **k: {"project": str(p), "release": "RZG3L-BSP-1.0.0", "state": "usable", "problems": [],
                                 "warnings": [], "components": {}, "running": False, "setup": None}
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
check("实现（第 5 步）" in stage(), "tag design-v1 -> step 5: implement")

# ---- steps 5 and 6: gates read from the PoC repository and the run reports (temporary dirs)
import json, time, datetime as dt
RUNS = Path("/tmp/wf-tests/runs"); shutil.rmtree(RUNS, ignore_errors=True)
os.environ["RZ_BUILD_RUNS"], os.environ["RZ_BOARD_RUNS"] = str(RUNS / "build"), str(RUNS / "board")
git = lambda *a: subprocess.run(["git", "-C", str(new), *a], check=True, capture_output=True)
def commit(msg): git("add", "-A"); git("commit", "-q", "-m", msg)
def iso(delta): return dt.datetime.fromtimestamp(time.time() + delta).isoformat(timespec="seconds")
def report(kind, name, **kw):
    d = RUNS / kind / name; d.mkdir(parents=True, exist_ok=True); (d / "report.json").write_text(json.dumps(kw))
WIC = "lpdemo-image-smarc-rzg3l.rootfs-20261126000000.wic.gz"
(new / "layers/meta-rzg3l-x/conf").mkdir(parents=True); (new / "layers/meta-rzg3l-x/conf/layer.conf").write_text("# layer\n")
(new / "build-conf").mkdir(); (new / "build-conf/layers").write_text("rzg3l-lowpower-display/layers/meta-rzg3l-x\n")
check("还缺：build-conf/image" in W.poc_prompt("lowpower-display"), "implement: a missing build-conf/image is named")
(new / "build-conf/image").write_text("lpdemo-image\n"); commit("step 5: implement")
check("构建（第 5 步）" in stage(), "implemented -> build")
report("build", "1-old", conf={"poc": new.name}, result="OK", start=iso(-3600), produced=[WIC], errors=[])
check("构建（第 5 步）" in stage(), "a build older than the last commit does not count")
report("build", "2-fail", conf={"poc": new.name}, result="FAILED", start=iso(5), produced=[], errors=["ERROR: x"])
check("上一次构建失败" in W.poc_prompt("lowpower-display"), "a failed build is reported with its errors")
report("build", "3-other", conf={"poc": "rzg3l-other"}, result="OK", start=iso(6), produced=[WIC], errors=[])
check("上一次构建失败" in W.poc_prompt("lowpower-display"), "another PoC's build does not count")
report("build", "4-ok", conf={"poc": new.name}, result="OK", start=iso(7), produced=["x.bin", WIC], errors=[])
s = W.poc_prompt("lowpower-display")
check("写板子（第 6 步）" in s and WIC in s and "evk-flash doctor" in s, "built -> flash, with the image of that build")
report("board", "1-wrong", command="rootfs", result="OK", start=iso(8), written=[{"file": "other.wic.gz"}])
check("写板子（第 6 步）" in stage(), "flashing another image does not count")
report("board", "2-ok", command="rootfs", result="OK", start=iso(9), written=[{"file": WIC}])
check("上板验证（第 6 步）" in stage(), "flashed this build's image -> verify")
v = new / "design/03-verification.md"
v.write_text("# x\n\n结果：1 项未通过（5/6）\n"); os.utime(v, (time.time() + 20, time.time() + 20))
check("上一次验证有未通过的项" in W.poc_prompt("lowpower-display"), "a failed verification stops; the user decides")
v.write_text(v.read_text() + "\n用户已确认接受：V-4（理由）\n"); os.utime(v, (time.time() + 20, time.time() + 20))
check("交付（第 5 步，打包）" in stage(), "accepted by the user -> package")
(new / "dist/pkg").mkdir(parents=True); (new / "dist/pkg/SHA256SUMS").write_text(""); os.utime(new / "dist/pkg/SHA256SUMS", (time.time() + 30, time.time() + 30))
check("用户确认交付物" in stage(), "packaged -> the user checks the deliverables")
git("tag", "release-v1")
s = W.poc_prompt("lowpower-display")
check("全部完成" in s and s.count("✓") >= 14, "tag release-v1 -> done; the status table shows steps 5-6")
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
