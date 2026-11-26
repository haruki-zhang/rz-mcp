# Run: python3 eval/test_yocto_project.py      (writes only under /home/mpu-epmaj/max/.rz-mcp-tests and /tmp/yp-tests;
#                                               the multi-os cases clone github.com/renesas-rz/meta-rz-multi-os)
"""Known-answer tests for setting up / reusing a Yocto project (workflow/build/yocto-project, yocto_setup.py,
rzmcp.yocto.assess, step 0 of the poc prompt, the project parameter of the BSP tools).

Fake projects are assembled from rzg3l-bsp-v1 with symlinks, so no layer is cloned except multi-os. Expected
results: the official layers of rzg3l-bsp-v1 match RZG3L-BSP-1.0.0 (checked by hand 2026-11-25); its
meta-rz-graphics / meta-rz-codecs were unpacked from the same packages, and its meta-rzg3l is multi-os v4.0.0
(both compared by hand 2026-11-26), so a fresh install must be identical to them."""
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
REAL = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1")
W = Path("/home/mpu-epmaj/max/.rz-mcp-tests")          # visible to the build container (it mounts /home/mpu-epmaj/max)
T = Path("/tmp/yp-tests")                              # not visible to the container
os.environ["RZ_POC_REGISTRY"] = str(T / "poc-projects.json")
sys.path.insert(0, str(RZ / "src"))
sys.path.insert(0, str(RZ / "workflow" / "build"))
from rzmcp import bsp as B        # noqa: E402
from rzmcp import workflow as WF  # noqa: E402
from rzmcp import yocto as Y      # noqa: E402
import yocto_setup as S           # noqa: E402

YP = RZ / "workflow" / "build" / "yocto-project"
fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    if not c:
        fails.append(m)


def run(*a):
    return subprocess.run([sys.executable, *map(str, a)], capture_output=True, text=True)


def listing(p: Path) -> list[str]:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*")) if p.exists() else []


def same_tree(a: Path, b: Path) -> bool:
    return subprocess.run(["diff", "-rq", str(a), str(b)], capture_output=True).returncode == 0


def fake_project(p: Path, poky_version: str | None = None, kernel_rev: str | None = None, downloads: bool = True) -> Path:
    """A project made of symlinks to rzg3l-bsp-v1's official layers (meta-renesas / meta-poky copied when changed)."""
    p.mkdir(parents=True)
    for l in ("meta-arm", "meta-openembedded"):
        (p / l).symlink_to(REAL / l)
    if poky_version:
        (p / "poky").mkdir()
        for e in (REAL / "poky").iterdir():
            if e.name != "meta-poky":
                (p / "poky" / e.name).symlink_to(e)
        shutil.copytree(REAL / "poky" / "meta-poky", p / "poky" / "meta-poky")
        f = p / "poky/meta-poky/conf/distro/poky.conf"
        f.write_text(f.read_text().replace('DISTRO_VERSION = "5.0.14"', f'DISTRO_VERSION = "{poky_version}"'))
    else:
        (p / "poky").symlink_to(REAL / "poky")
    if kernel_rev:
        shutil.copytree(REAL / "meta-renesas", p / "meta-renesas", symlinks=True)
        f = p / "meta-renesas" / Y.release()["components"]["kernel"]["recipe"]
        t = f.read_text()
        assert Y.release()["components"]["kernel"]["rev"] in t
        f.write_text(t.replace(Y.release()["components"]["kernel"]["rev"], kernel_rev))
    else:
        (p / "meta-renesas").symlink_to(REAL / "meta-renesas")
    if downloads:
        (p / "build").mkdir()
        (p / "build" / "downloads").symlink_to(REAL / "build" / "downloads")
    return p


def hold_lock(p: Path):
    """Another process holding <p>/.rz-mcp/setup.lock, as a running setup does."""
    (p / Y.STATE_DIR).mkdir(parents=True, exist_ok=True)
    code = ("import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.lockf(f, fcntl.LOCK_EX); "
            "print('locked', flush=True); time.sleep(120)")
    pr = subprocess.Popen([sys.executable, "-c", code, str(p / Y.STATE_DIR / "setup.lock")], stdout=subprocess.PIPE, text=True)
    pr.stdout.readline()
    return pr


for d in (W, T):
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)

# ---------------------------------------------------------------- 1. classification
a = Y.assess(REAL)
check(a["state"] == "usable" and not a["problems"], f"rzg3l-bsp-v1 -> usable ({a['problems']})")
check(sum("not a git checkout" in w for w in a["warnings"]) == 4, "rzg3l-bsp-v1: the 4 tarball layers are reported, not refused")
check(set(a["components"]) == {"graphics", "codecs", "multi-os"}, f"rzg3l-bsp-v1: graphics, codecs, multi-os (old layout) present: {a['components']}")
check(Y.assess(W / "nope")["state"] == "absent", "missing directory -> absent")
(W / "empty").mkdir()
check(Y.assess(W / "empty")["state"] == "absent", "empty directory -> absent")
e2 = W / "empty2"
(e2 / ".claude").mkdir(parents=True)
(e2 / ".claude" / "settings.local.json").write_text("{}")
(e2 / "rzg3l-x" / "design").mkdir(parents=True)
subprocess.run(["git", "init", "-q", str(e2 / "rzg3l-x")], check=True)
check(Y.assess(e2)["state"] == "absent", "only .claude/ and a PoC repository -> still absent (can be set up)")
(W / "junk").mkdir()
(W / "junk" / "notes.txt").write_text("x")
a = Y.assess(W / "junk")
check(a["state"] == "unusable" and "not empty" in a["problems"][0], "a directory with other files -> unusable")
(T / "empty").mkdir()
a = Y.assess(T / "empty")
check(a["state"] == "unusable" and any("does not mount" in p for p in a["problems"]), "outside the container's mounts -> unusable")
P1 = fake_project(W / "p1")
a = Y.assess(P1)
check(a["state"] == "usable" and any("no build/conf" in w for w in a["warnings"]), "assembled official layers -> usable; build/conf missing is a warning")
a = Y.assess(fake_project(W / "oldkernel", kernel_rev="0" * 40, downloads=False))
check(a["state"] == "unusable" and any("pins kernel" in p for p in a["problems"]), "meta-renesas pinning another kernel -> unusable (another release)")
a = Y.assess(fake_project(W / "oldpoky", poky_version="5.0.9", downloads=False))
check(a["state"] == "unusable" and any("DISTRO_VERSION 5.0.9" in p for p in a["problems"]), "another poky version -> unusable")
a = Y.assess(fake_project(W / "nofetch", downloads=False))
check(a["state"] == "usable" and sum("not fetched" in w for w in a["warnings"]) == 3, "no git mirrors yet -> usable, 3 'not fetched' warnings")
inc = W / "inc"
(inc / Y.STATE_DIR).mkdir(parents=True)
(inc / Y.STATE_DIR / "project.json").write_text(json.dumps({"created_by": "yocto-project", "done": None}))
(inc / "poky").mkdir()
a = Y.assess(inc)
check(a["state"] == "incomplete" and not a["running"], "an unfinished setup -> incomplete, not running")
pr = hold_lock(inc)
check(Y.assess(inc)["running"], "a held setup.lock -> running")

# ---------------------------------------------------------------- 2. CLI: check / refusals change nothing
check(run(YP, "check", REAL).returncode == 0, "check: usable -> exit 0")
check(run(YP, "check", W / "nope").returncode == 1, "check: absent -> exit 1")
check(run(YP, "check", W / "junk").returncode == 2, "check: unusable -> exit 2")
before = listing(W / "junk")
r = run(YP, "setup", W / "junk", "--no-fetch")
check(r.returncode == 2 and listing(W / "junk") == before, "setup refuses an unusable directory and leaves it as it was")
r = run(YP, "setup", inc, "--no-fetch")
check(r.returncode == 3, f"setup refuses while another setup holds the lock (exit {r.returncode})")
pr.kill()
pr.wait()
check(run(YP, "add", W / "nope", "graphics").returncode == 2 and not (W / "nope").exists(), "add refuses a missing project")
check(run(YP, "setup", "relative/dir").returncode != 0, "a relative directory is refused")
r = run(YP, "add", P1, "no-such")
check(r.returncode == 2 and not (P1 / "meta-rz-features").exists(), "unknown component -> refused before anything changes")

# ---------------------------------------------------------------- 3. optional components
r = run(YP, "add", P1, "graphics", "codecs", "security")
check(r.returncode == 0, f"add graphics codecs security ({r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]})")
check(same_tree(P1 / "meta-rz-features/meta-rz-graphics", REAL / "meta-rz-features/meta-rz-graphics"),
      "graphics from the package == rzg3l-bsp-v1/meta-rz-features/meta-rz-graphics")
check(same_tree(P1 / "meta-rz-features/meta-rz-codecs", REAL / "meta-rz-features/meta-rz-codecs"),
      "codecs (with the prebuilt archive) == rzg3l-bsp-v1/meta-rz-features/meta-rz-codecs")
check((P1 / "meta-rz-features/meta-rz-security/conf/layer.conf").exists() and
      (P1 / "meta-rz-features/meta-rz-security/classes/rz-security.bbclass").exists(), "security layer unpacked")
st = Y.read_state(P1)
check(st and st["components"]["graphics"]["sha256"] == Y.release()["optional"][0]["sha256"] and
      st.get("created_by") is None, "recorded in .rz-mcp/project.json with the sha256; a reused project is not marked as created")
check(not (P1 / Y.STATE_DIR / "stage").exists() or not any((P1 / Y.STATE_DIR / "stage").iterdir()), "no staging left behind")
before = listing(P1 / "meta-rz-features")
r = run(YP, "add", P1, "graphics")
check(r.returncode == 0 and "already present" in r.stdout and listing(P1 / "meta-rz-features") == before, "adding again changes nothing")
a = Y.assess(P1)
check("installed by yocto-project" in a["components"].get("graphics", ""), "assess shows it as installed by yocto-project")

c = next(c for c in Y.optional_components() if c["name"] == "graphics")
P2 = fake_project(W / "p2")
try:
    S.install_package(P2, {**c, "sha256": "0" * 64})
    check(False, "a sha256 mismatch is refused")
except S.SetupError as e:
    check("sha256" in str(e) and not (P2 / "meta-rz-features").exists(), "a sha256 mismatch is refused; nothing written")
try:
    S.install_package(P2, {**c, "file": str(T / "missing.zip")})
    check(False, "a missing package file is refused")
except S.SetupError as e:
    check("is missing" in str(e) and "renesas.com" in str(e), "a missing package names what to download")
evil = T / "evil.zip"
tb = io.BytesIO()
with tarfile.open(fileobj=tb, mode="w:gz") as tf:
    for name in ("meta-rz-features/meta-rz-x/conf/layer.conf", "../outside.txt"):
        data = b"x"
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("pkg/evil.tar.gz", tb.getvalue())
import hashlib  # noqa: E402
bad = {"name": "evil", "kind": "package", "part": "X", "version": "1", "file": str(evil),
       "sha256": hashlib.sha256(evil.read_bytes()).hexdigest(), "extract": ["pkg/evil.tar.gz"], "layers": ["meta-rz-features/meta-rz-x"]}
try:
    S.install_package(P2, bad)
    check(False, "a member outside meta-rz-features/ is refused")
except S.SetupError as e:
    check(not (W / "outside.txt").exists() and not (P2 / "outside.txt").exists() and not (P2 / "meta-rz-features").exists(),
          f"a member outside meta-rz-features/ is refused, nothing escapes ({type(e).__name__})")

r = run(YP, "add", P1, "multi-os")
check(r.returncode == 0, f"add multi-os (git) ({r.stderr.strip()[-300:]})")
mos = P1 / "meta-rz-features/meta-rz-multi-os"
head = subprocess.run(["git", "-C", str(mos), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
check(head == "8edaac208c3f9757e63852bc761d08c47653248c", "multi-os checked out at v4.0.0 (8edaac2)")
check(same_tree(mos / "meta-rzg3l", REAL / "meta-rz-features/meta-rz-multi-os/meta-rzg/meta-rzg3l"),
      "multi-os meta-rzg3l == rzg3l-bsp-v1's (old layout)")
other = W / "other-checkout"
subprocess.run(["git", "init", "-q", str(other)], check=True)
subprocess.run(["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
try:
    S.clone_at(P2, "https://example.invalid/x", "1" * 40, other, "x")
    check(False, "an existing checkout at another revision is refused")
except S.SetupError as e:
    check("not touching it" in str(e), "an existing checkout at another revision is refused, untouched")

# ---------------------------------------------------------------- 4. setup reuses a usable project; poc-use adds components
r = run(YP, "setup", P1, "--no-fetch")
check(r.returncode == 0 and "reused" in r.stdout and (P1 / "build/conf/bblayers.conf").exists(),
      "setup on a usable project: reused, only build/conf (baseline) added")
check(Y.read_state(P1).get("created_by") is None, "reuse does not claim the project as created")
P3 = fake_project(W / "p3")
poc = P3 / "rzg3l-yp"
(poc / "build-conf").mkdir(parents=True)
(poc / "build-conf/layers").write_text("meta-rz-features/meta-rz-codecs\nmeta-rz-features/meta-rz-multi-os/meta-rzg/meta-rzg3l   # old layout\n")
r = run(RZ / "workflow/build/poc-use", poc, "--project", P3, "--dry-run")
check(r.returncode == 0 and "would install the optional component codecs" in r.stderr and not (P3 / "meta-rz-features").exists(),
      "poc-use --dry-run reports the missing components and installs nothing")
r = run(RZ / "workflow/build/poc-use", poc, "--project", P3)
bbl = (P3 / "build/conf/bblayers.conf").read_text() if (P3 / "build/conf/bblayers.conf").exists() else ""
check(r.returncode == 0 and "meta-rz-features/meta-rz-codecs" in bbl and "meta-rz-multi-os/meta-rzg3l" in bbl
      and "meta-rzg/meta-rzg3l" not in bbl, f"poc-use installs codecs and multi-os, maps the old layout path ({r.stderr.strip()[-300:]})")
(poc / "build-conf/layers").write_text("meta-not-a-layer\n")
r = run(RZ / "workflow/build/poc-use", poc, "--project", P3)
check(r.returncode != 0 and "is not a layer" in r.stderr, "an unknown missing layer is still an error")

# ---------------------------------------------------------------- 5. the BSP tools take a project
out = B.check_build_env(str(P1))
check("project state: usable" in out and "optional graphics: 5.1.3.2 (installed by yocto-project" in out, "check_build_env(project) reads that project")
src = B.bsp_source("kernel", "Makefile", 1, 5, project=str(P1))
check("lines 1-5" in src, "bsp_source(project) reads the project's git mirror")
check(f"Checked against the Yocto project {P1}" in B.get_bsp_release(project=str(P1)), "get_bsp_release(project)")
for bad in ("relative/x", str(W / "nope")):
    try:
        B.bsp_grep("kernel", "x", project=bad)
        check(False, f"bsp_grep refuses project {bad!r}")
    except ValueError:
        check(True, f"bsp_grep refuses project {bad!r}")
out = B.check_build_env(str(W / "junk"))
check("FAIL project state: unusable" in out and "yocto-project setup" not in out, "check_build_env on an unusable directory: no setup advice")

# ---------------------------------------------------------------- 6. step 0 of the poc prompt
newdir = W / "new-prj"
t = WF.poc_prompt("yp-a", str(newdir))
check("准备 Yocto 项目（第 0 步）" in t and f"yocto-project setup {newdir}" in t and "不存在" in t, "new directory -> step 0: set it up")
check(Y.registry().get("yp-a") == str(newdir), "the directory is registered for the PoC")
check(f"Yocto 项目：`{newdir}`" in WF.poc_prompt("yp-a"), "later calls without a directory use the registered one")
t = WF.poc_prompt("yp-b", str(W / "junk"))
check("不能用作 Yocto 项目" in t and "不要改动它" in t, "unusable directory -> stop, do not touch")
check("不合法" not in WF.poc_prompt("yp-c", "relative/dir") and "absolute path" in WF.poc_prompt("yp-c", "relative/dir"),
      "relative directory -> error text")
pr = hold_lock(inc)
t = WF.poc_prompt("yp-d", str(inc))
check("正在后台搭建" in t and "创建 PoC 仓库" in t, "setup running + PoC at step 1 -> step 1 goes on meanwhile")
d = inc / "rzg3l-yp-d" / "design"
d.mkdir(parents=True)
subprocess.run(["git", "init", "-q", str(inc / "rzg3l-yp-d")], check=True)
for f in ("00-request.md", "00a-restated.md", "00b-sharpened.md"):
    (d / f).write_text("filled\n")
check("## 等待：Yocto 项目" in WF.poc_prompt("yp-d"), "setup running + PoC past step 1 -> wait for it")
pr.kill()
pr.wait()
check("上次的搭建没有完成" in WF.poc_prompt("yp-d") and f"yocto-project setup {inc}" in WF.poc_prompt("yp-d"),
      "setup interrupted -> run it again to continue")
t = WF.poc_prompt("yp-e", str(fake_project(W / "p5", downloads=False)))
check("可用，但还缺" in t and "复用" in t, "usable project missing build/conf and sources -> step 0 completes it (reuse)")
# a usable project with everything: step 2 names $PAST and where the session must be opened
d = P1 / "rzg3l-yp-f" / "design"
d.mkdir(parents=True)
subprocess.run(["git", "init", "-q", str(P1 / "rzg3l-yp-f")], check=True)
for f in ("00-request.md", "00a-restated.md", "00b-sharpened.md"):
    (d / f).write_text("filled\n")
t = WF.poc_prompt("yp-f", str(P1))
check("两轨独立设计" in t and f"`$PAST` = `{REAL}`" in t and f"在 `{P1}` 下重新打开 Claude Code" in t
      and f"install-agents --project {P1}" in t, "step 2 in another project: $PAST, session location, install-agents --project")
t = WF.poc_prompt("yp-f", str(W / "new-prj"))
check("## 停下" in t and "已经在" in t, "the same PoC name with another directory -> stop (it lives elsewhere)")
t = WF.poc_prompt("evk-mac")
check("准备 Yocto 项目" not in t and f"--project {REAL}" in t, "default project (rzg3l-bsp-v1) -> no step 0; commands carry --project")

for d in (W, T):
    shutil.rmtree(d, ignore_errors=True)
print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
