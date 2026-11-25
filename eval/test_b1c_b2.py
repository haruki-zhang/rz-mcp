# Run: /usr/bin/python3 eval/test_b1c_b2.py      (reads the real Yocto project; writes only under /tmp/b2-tests)
"""Known-answer tests for B1c (rzmcp.bsp) and B2 (workflow/build). Expected values are computed independently
(git, the deploy directory, rzg3l-lowpower-display) or were verified by hand on 2026-11-25."""
import fcntl
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RZ / "src"))
from rzmcp import bsp as B   # noqa: E402
from rzmcp import yocto as Y  # noqa: E402

P = Y.project_dir()
D = P / "build" / "tmp" / "deploy" / "images" / "smarc-rzg3l"
REF = P / "rzg3l-lowpower-display"
W = Path("/tmp/b2-tests")
IMG = sorted(D.glob("lpdemo-image-smarc-rzg3l.rootfs-*.wic.gz"))[-1]
fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    if not c:
        fails.append(m)


def run(*a, **kw):
    return subprocess.run([sys.executable, *map(str, a)], capture_output=True, text=True, **kw)


shutil.rmtree(W, ignore_errors=True)
W.mkdir()

# ---------------- B1c
rel = B.get_bsp_release()
for want in ("7e8674996b0164b07e56bc066d0fba790e627061", "89a01c3d9ad1f8fce6aeb4dd0e694cfa28d42099",
             "RZG3L-BSP-1.0.0 = 4823845d3a9a", "rev e6c894b7436f14ba9ddda9e5807d3e19e9e2e803, KERNEL_BRANCH=rz-6.12-cip8, LINUX_VERSION=6.12.46-cip8",
             "rev 16e6324c9125c6c99e762214137c47d9d7b3ea7f", "rev c06209f12ff5d3a94e674a7f46fd01ec76356928",
             "RTK0EF0045Z14001ZJ", "sector 0x1380", "at 0x60000"):
    check(want in rel, f"get_bsp_release: {want}")
check("!!" not in rel and "MISSING" not in rel, "get_bsp_release: project pins = release, mirrors have the revisions")

mirror = P / "build" / "downloads" / "git2" / "github.com.renesas-rz.rz_linux-cip.git"
rev = "e6c894b7436f14ba9ddda9e5807d3e19e9e2e803"
want = subprocess.run(["git", f"--git-dir={mirror}", "show", f"{rev}:drivers/watchdog/rzg2l_wdt.c"], capture_output=True, text=True).stdout.splitlines()
got = B.bsp_source("kernel", "drivers/watchdog/rzg2l_wdt.c", 1, 2000).splitlines()[1:]
check([g[8:] for g in got[:len(want)]] == want, f"bsp_source: rzg2l_wdt.c = git show at the official rev ({len(want)} lines)")
check("(directory" in B.bsp_source("u-boot", "configs"), "bsp_source: a directory is listed")
ws = subprocess.run(["git", f"--git-dir={mirror}", "grep", "-n", "-E", "-e", "renesas,r9a08g046-gbeth", rev, "--", "drivers"], capture_output=True, text=True).stdout.splitlines()
g = B.bsp_grep("kernel", "renesas,r9a08g046-gbeth", "drivers")
check(len(ws) > 0 and f"{len(ws)} hit(s)" in g, f"bsp_grep: same hits as git grep ({len(ws)})")
for bad in ("../../etc/passwd", "-o/tmp/x"):
    try:
        B.bsp_source("kernel", bad)
        check(False, f"bsp_source refuses {bad!r}")
    except ValueError:
        check(True, f"bsp_source refuses {bad!r}")
check("(no hits)" in B.bsp_grep("tf-a", "--output=/tmp/x"), "bsp_grep: a pattern starting with '-' is a pattern, not an option")

ks = P / "build" / "tmp" / "work-shared" / "smarc-rzg3l" / "kernel-source"
n = len(subprocess.run(["git", "-C", str(ks), "log", "--format=%h", f"{rev}..HEAD"], capture_output=True, text=True).stdout.split())
ps = B.patch_stack("kernel")
check(f"{n} commit(s) on top" in ps and "NOT FOUND" not in ps, f"patch_stack kernel: all {n} commits of the last build traced to a layer patch")
check("meta-rzg3l-combo/0001-arm64-dts-renesas-rzg3l-smarc-fastboot-keep-USB-enabled.patch" in ps, "patch_stack: the combo layer's patch is found")
cb = B.check_build_env()
check("official layers present" in cb and "kernel: meta-renesas pins e6c894b7436f (release e6c894b7436f)" in cb,
      "check_build_env: official layers and component pins")
check("DISTRO = rz-vlp-fast (defined in meta-rzg3l-fast-boot)" in cb and "PoC repository rzg3l-lowpower-display" in cb,
      "check_build_env: DISTRO traced to its layer; extra layers traced to PoC repositories")

# ---------------- B2: poc-use round trip on the real build/conf (nothing real is written)
cap = W / "rzg3l-capture"
cap.mkdir()
r = run(RZ / "workflow/build/poc-use", "--capture", cap)
nb = W / "build"
(nb / "conf").mkdir(parents=True)
r2 = run(RZ / "workflow/build/poc-use", cap, "--build-dir", nb)
cur = P / "build"
check(r.returncode == 0 and r2.returncode == 0, f"poc-use: capture and apply ({r.stderr[-200:]}{r2.stderr[-200:]})")
check(Y.bblayers(cur / "conf/bblayers.conf", cur) == Y.bblayers(nb / "conf/bblayers.conf", nb), "poc-use: layers identical, same order")
sc, sn = Y.statements((cur / "conf/local.conf").read_text()), Y.statements((nb / "conf/local.conf").read_text())
check(set(sc) <= set(sn) and set(sn) - set(sc) <= {'RM_WORK_EXCLUDE += "linux-renesas u-boot trusted-firmware-a"'},
      "poc-use: every local.conf statement kept; only the standard RM_WORK_EXCLUDE added")
for v in ("MACHINE", "DISTRO", "USE_OPENAMP"):
    check(Y.conf_value(sc, v) == Y.conf_value(sn, v), f"poc-use: effective {v} unchanged ({Y.conf_value(sn, v)})")
check(json.loads((nb / "conf/poc-use.json").read_text())["poc"] == "rzg3l-capture", "poc-use: poc-use.json records the PoC")
r3 = run(RZ / "workflow/build/poc-use", cap, "--build-dir", nb)
check(len(list((nb / "conf/backup").iterdir())) == 1, "poc-use: the previous build/conf is backed up")
with open(nb / "bitbake.lock", "w") as lk:
    fcntl.lockf(lk, fcntl.LOCK_EX)
    r4 = run(RZ / "workflow/build/poc-use", cap, "--build-dir", nb)
check(r4.returncode != 0 and "bitbake is running" in r4.stderr, "poc-use: refuses while bitbake holds bitbake.lock")
(cap / "build-conf/layers").write_text("rzg3l-lowpower-display/docs\n")
r5 = run(RZ / "workflow/build/poc-use", cap, "--build-dir", nb, "--dry-run")
check(r5.returncode != 0 and "is not a layer" in r5.stderr, "poc-use: a path that is not a layer is refused")

# ---------------- B2: poc-package of rzg3l-lowpower-display (known layout)
src = W / "rzg3l-lowpower-display"
shutil.copytree(REF, src, symlinks=True)
(src / "design").mkdir()
(src / "design" / "secret.md").write_text("internal")
r = run(RZ / "workflow/build/poc-package", src, "--image", IMG, "--no-pdf", timeout=600)
out = src / "dist" / "rzg3l-lowpower-display-v1.0"
check(r.returncode == 0, f"poc-package runs ({r.stderr[-300:]})")
top = lambda p: sorted(x.name for x in p.iterdir() if not x.name.startswith("."))
check(top(out) == sorted(top(REF) + ["SHA256SUMS", "versions.json"]), f"poc-package: layout = rzg3l-lowpower-display + 2 files ({top(out)})")
check(not (out / "design").exists(), "poc-package: design/ is not delivered")
check(all((out / f).read_bytes() == (REF / f).read_bytes() for f in ("README.md", "docs/RZG3L-LowPower-Display-Guide.md", "tools/md2pdf.py")),
      "poc-package: README, guide and md2pdf copied unchanged")
check(sorted(x.name for x in (out / "image").iterdir()) == ["README.md", "lpdemo-image-smarc-rzg3l.rootfs.wic.bmap", "lpdemo-image-smarc-rzg3l.rootfs.wic.gz"],
      "poc-package: image/ has the README and the image under its delivered name")
ok = all(hashlib.sha256((out / l.split("  ", 1)[1]).read_bytes()).hexdigest() == l.split("  ", 1)[0]
         for l in (out / "SHA256SUMS").read_text().splitlines())
check(ok, "poc-package: SHA256SUMS verifies")
v = json.loads((out / "versions.json").read_text())
check(v["components"]["kernel"]["rev"].startswith("e6c894b7") and v["image"]["sha256"] == hashlib.sha256(IMG.read_bytes()).hexdigest()
      and "fip-smarc-rzg3l.bin" in v["boot_files"], "poc-package: versions.json has component revs, image and boot file sha256")
skel = W / "rzg3l-skeleton"
r = run(RZ / "workflow/poc-new", "skeleton", "--project", W) if False else None
(skel / "docs").mkdir(parents=True)
(skel / "README.md").write_text("# x")
r = run(RZ / "workflow/build/poc-package", skel, "--image", IMG, "--no-pdf")
check(r.returncode != 0 and "not ready to deliver" in r.stderr, "poc-package: refuses a skeleton without guide and layers")

shutil.rmtree(W, ignore_errors=True)
print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
