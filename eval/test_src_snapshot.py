# Run: /usr/bin/python3 eval/test_src_snapshot.py   (reads the real projects and their snapshots; writes only /tmp/src-snapshot-tests)
"""Tests for the source snapshot (workflow/build/src-snapshot, rzmcp.snapshot) and the tools that read it
(bsp_source / bsp_grep views, patch_stack, build_facts, bsp_recipe).

Every expected value comes from outside rz-mcp's own code:
  - git show / tar -xO on the DL_DIR mirrors and layer tarballs, GNU patch (not git apply) for patched files;
  - tree hashes of bitbake's own do_patch output (scratch build of the same configuration, 2026-11-26);
  - the official kernel build of rzg3l-bsp (BR-0, build/tmp/work/.../linux-smarc_rzg3l-standard-build) and the
    evk-mac PoC records (design/research-*.md, 03-verification.md; MAIN transcript lines named below).
Needs: snapshots of rzg3l-bsp-v1 (with --artifacts) and rzg3l-bsp.
"""
import gzip
import re
import shutil
import subprocess
import sys
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RZ / "src"))
from rzmcp import bsp as B        # noqa: E402
from rzmcp import snapshot as S   # noqa: E402

V1 = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1")      # official + graphics, codecs, multi-os (+ PoC layers)
BSP = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp")        # official only (+ the evk-mac PoC layer)
W = Path("/tmp/src-snapshot-tests")
KREV, UREV, TREV = ("e6c894b7436f14ba9ddda9e5807d3e19e9e2e803", "16e6324c9125c6c99e762214137c47d9d7b3ea7f",
                    "c06209f12ff5d3a94e674a7f46fd01ec76356928")
fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    if not c:
        fails.append(m)


def body(out: str) -> list[str]:
    """The source lines of a bsp_source result (strip the line-number column)."""
    return [l[8:] for l in out.splitlines() if re.match(r"^ {0,5}\d+  ", l)]


def read_all(comp, path, project, view="release"):
    """Page through bsp_source (2000 lines per call) and return every source line."""
    out, start = [], 1
    while True:
        r = B.bsp_source(comp, path, start, 2000, project, view=view)
        out += body(r)
        m = re.search(r"start_line=(\d+)\)", r)
        if not m:
            return out
        start = int(m.group(1))


def raises(f, *a, **kw):
    try:
        f(*a, **kw)
        return None
    except ValueError as e:
        return str(e)


shutil.rmtree(W, ignore_errors=True)
W.mkdir()
sv1, sbsp = S.load(V1), S.load(BSP)
check(sv1 is not None and sbsp is not None, "snapshots of rzg3l-bsp-v1 and rzg3l-bsp exist")
if not (sv1 and sbsp):
    sys.exit(1)
check(not S.stale(sv1) and not S.stale(sbsp), "snapshots are not stale")
km = V1 / "build" / "downloads" / "git2" / "github.com.renesas-rz.rz_linux-cip.git"
um = V1 / "build" / "downloads" / "git2" / "github.com.renesas-rz.renesas-u-boot-cip.git"


def gshow(mirror, rev, path):
    return subprocess.run(["git", f"--git-dir={mirror}", "show", f"{rev}:{path}"], capture_output=True, text=True).stdout.splitlines()


# ---------------- 1. byte-identical to the raw material
f = "drivers/watchdog/rzg2l_wdt.c"
check(body(B.bsp_source("kernel", f, 1, 2000, V1, view="rev")) == gshow(km, KREV, f), f"rev view {f} = git show {KREV[:12]}")
check(body(B.bsp_source("kernel", f, 1, 2000, V1)) == gshow(km, KREV, f), f"release view of an unpatched file = git show")
f = "env/common.c"
check(body(B.bsp_source("u-boot", f, 1, 2000, BSP)) == gshow(um, UREV, f), "u-boot release view (no patches) = git show")


def patched(mirror_or_text, patches, path, strip=1, cwd_rel=""):
    """Reference for a patched file: the base text + GNU patch (independent of git apply)."""
    d = W / "p"
    shutil.rmtree(d, ignore_errors=True)
    (d / Path(path).parent).mkdir(parents=True)
    (d / path).write_text(mirror_or_text)
    for p in patches:
        r = subprocess.run(["patch", "-s", f"-p{strip}", "-i", p, "--no-backup-if-mismatch", "-f"], cwd=d / cwd_rel,
                           capture_output=True, text=True)
    return (d / path).read_text().splitlines()


kc = sv1["components"]["kernel"]
ostm = next(p for p in kc["patches"] if "OSTM2" in p["name"])
ostm_files = [f for f, ps in kc["changed"].items() if ps == [ostm["name"]] and gshow(km, KREV, f)]
check(ostm_files == ["drivers/clk/renesas/r9a08g046-cpg.c"], f"OSTM2 patch is the only one changing an existing file: {ostm_files}")
f = "drivers/clk/renesas/r9a08g046-cpg.c"
ref = patched("\n".join(gshow(km, KREV, f)) + "\n", [ostm["path"]], f)
out = B.bsp_source("kernel", f, 1, 2000, V1)
check(read_all("kernel", f, V1) == ref, f"release view {f} = git show + GNU patch of {ostm['name']}")
check("changed by the official patch(es): 0002-Set-OSTM2-as-critical-clock.patch" in out, "bsp_source names the patch that changed the file")
check(read_all("kernel", f, V1, "rev") == gshow(km, KREV, f) != ref, "the same file in the rev view is unpatched")

mc = sv1["components"].get("mali", {})
check(mc.get("kind") == "tar", "mali is served from its tarball")
if mc.get("kind") == "tar":
    sub = mc["subdir"]                           # S inside the tarball (patches apply there)
    f = "mali_kbase_core_linux.c"
    raw = subprocess.run(["tar", "-xzOf", mc["tarball"], f"{sub}/{f}"], capture_output=True, text=True).stdout
    check(read_all("mali", f, V1, "rev") == raw.splitlines(), f"mali rev view = tar -xO of the tarball ({len(raw.splitlines())} lines)")
    mp = [p["path"] for p in mc["patches"] if f in [x for x, ps in mc["changed"].items() if p["name"] in ps]]
    ref = patched(raw, mp, f, strip=5)
    check(read_all("mali", f, V1) == ref != raw.splitlines(), f"mali release view = tar -xO + GNU patch ({len(mp)} patches)")

# ---------------- 2. release views = bitbake do_patch (tree hashes of the scratch build of the same configuration)
DO_PATCH = {"kernel": "3251351fe25487500c1e482fbf6312fe5bdd1393", "tf-a": "1f85f152adeac9e7ea63662746c37c0481ae647e",
            "u-boot": "dd16b4d7b269ff0da0d2396322f3385c2bde3545", "mali": "3e1535d8bb358a609cbafef79ef8c3cadfe2d0a4"}
for comp, tree in DO_PATCH.items():
    got = sv1["components"].get(comp, {}).get("release_tree")
    check(got == tree, f"v1 release view of {comp} = bitbake do_patch tree {tree[:12]} (got {got and got[:12]})")
kb = BSP / "build/tmp/work-shared/smarc-rzg3l/kernel-source"
ks_tree = subprocess.run(["git", "-C", str(kb), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
check(sbsp["components"]["kernel"]["release_tree"] == ks_tree,
      "rzg3l-bsp release kernel = its own official build's kernel-source (BR-0, no PoC kernel patch)")

# ---------------- 2b. artifacts built in the scratch directory = the project's own official build (BR-0)
br0 = BSP / "build/tmp/work/smarc_rzg3l-poky-linux/linux-renesas/6.12.46-cip8+git/linux-smarc_rzg3l-standard-build"
sa = Path(sbsp["_dir"])
if sbsp.get("artifacts", {}).get("kernel"):
    check((sa / "kernel/.config").read_bytes() == (br0 / ".config").read_bytes(), "rzg3l-bsp snapshot kernel .config = BR-0 build .config")
    check((sa / "kernel/r9a08g046l48-smarc.dtb").read_bytes() == (br0 / "arch/arm64/boot/dts/renesas/r9a08g046l48-smarc.dtb").read_bytes(),
          "rzg3l-bsp snapshot DTB = BR-0 DTB (byte-identical)")
    mine = set((sa / "kernel/objects.txt").read_text().split())
    theirs = {str(q.relative_to(br0)) for q in br0.rglob("*.o") if not q.name.startswith(".") and not q.name.endswith(".mod.o")
              and "built-in" not in q.name}
    check(mine == theirs, f"rzg3l-bsp snapshot compiled objects = BR-0 objects ({len(mine)} / {len(theirs)})")
else:
    check(False, "rzg3l-bsp snapshot has build artifacts (src-snapshot --artifacts)")

# ---------------- 3. the project was not written: no object of the views duplicates a mirror object
views = Path(sv1["_dir"]) / "views.git"
loose = [p for p in (views / "objects").glob("??/*")]
dup = [p for p in loose[:400] if subprocess.run(["git", f"--git-dir={km}", "cat-file", "-e", p.parent.name + p.name],
                                                  capture_output=True).returncode == 0]
check(not dup, f"views.git holds only new objects ({len(loose)} loose, none already in the kernel mirror)")

# ---------------- 4. patch_stack = the resolved SRC_URI (B2 of the demand analysis)
ps = B.patch_stack("u-boot", BSP)
check("patches: none" in ps and "0001-board-rzg3l-derive-static-MAC" in ps.split("Not part of the official view")[1],
      "patch_stack u-boot (official): no patch; the evk-mac patches are listed apart (PoC conclusion, MAIN:712)")
check("CVE-2024" not in ps and "hihope" not in ps, "patch_stack no longer lists patch files that are not applied")
ps = B.patch_stack("kernel", V1)
for n in ("0004-GPU-driver-remove-power-domains", "0001-Add-rzg3l-smarc-multi-os-dtsi", "0002-Set-OSTM2-as-critical-clock"):
    check(n in ps, f"patch_stack kernel (v1): official patch {n} (graphics / multi-os)")

# ---------------- 5. paths and windows (B3, B1)
e = raises(B.bsp_grep, "kernel", "eqos", "drivers/net/dwc_eth_qos", 5, BSP)
check(e and "does not exist" in e, "bsp_grep: a path that does not exist is an error, not '(no hits)'")
g = B.bsp_grep("kernel", "ethernet", "arch/arm64/boot/dts/renesas/r9a08g046", 5, BSP)
check("used as a prefix" in g and "hit(s)" in g, "bsp_grep: a partial path is used as a prefix and says so")
g = B.bsp_grep("u-boot", "eth_env_set_enetaddr", "env/common.c lib/hashtable.c", 5, BSP)
check("env/common.c:202:" in g, "bsp_grep: several paths separated by spaces")
s = B.bsp_source("u-boot", "env/common.c", 190, 16, BSP)
check("ends INSIDE a block opened at line 202" in s, "bsp_source warns when the window stops inside eth_env_set_enetaddr (S-14)")

# ---------------- 6. known-answer questions (answers from build artifacts, the board and the PoC records)
def bf(*a, **kw):
    return B.build_facts(*a, project=V1, **kw)


qa = [
    # (question, call, expected regex, where the answer comes from)
    ("Is STRICT_DEVMEM enabled in the official kernel?", lambda: bf("config", "STRICT_DEVMEM"), r"CONFIG_STRICT_DEVMEM=y",
     "BR-0 kernel-build-artifacts/.config; devmem check on the board, evk-mac V-18.1"),
    ("U-Boot: is CONFIG_EVENT set?", lambda: bf("config", "EVENT$", component="u-boot"), r"CONFIG_EVENT=y",
     "evk-mac MAIN:1043 (the built U-Boot .config)"),
    ("U-Boot: SHA256 / OF_SYSTEM_SETUP / DM_SEQ_ALIAS", lambda: bf("config", "^CONFIG_(SHA256|OF_SYSTEM_SETUP|DM_SEQ_ALIAS)$", component="u-boot"),
     r"(?s)CONFIG_SHA256=y.*|CONFIG_OF_SYSTEM_SETUP=y", "evk-mac MAIN:1043"),
    ("Is the i3c node enabled?", lambda: bf("dt", "i3c"), r"STATUS: disabled", "BR-0 compiled DTB (override sits in #if SW_I3C_EN, which is 0)"),
    ("Is sdhi2 enabled?", lambda: bf("dt", "sdhi2"), r"STATUS: disabled", "BR-0 compiled DTB (#if SW_SD2_DEV_SEL = 0)"),
    ("Is i2c0 enabled?", lambda: bf("dt", "i2c0"), r"STATUS: okay", "BR-0 compiled DTB (&i2c0 in the board dts)"),
    ("Is the GPU node enabled?", lambda: bf("dt", "renesas,r9a08g046-mali"), r"STATUS: okay", "BR-0 compiled DTB"),
    ("Is panfrost (the in-tree driver matching arm,mali-bifrost) compiled?",
     lambda: bf("compiled", "drivers/gpu/drm/panfrost/panfrost_drv.c"), r"NOT compiled", "BR-0 objects: no panfrost_drv.o"),
    ("Is the R-Car CAN-FD driver compiled in the official kernel?", lambda: bf("compiled", "drivers/net/can/rcar/rcar_canfd.c"),
     r": COMPILED", "BR-0 objects (the v1 PoC build drops it: 906 objects fewer)"),
    ("Is UIO compiled (multi-os installed)?", lambda: bf("config", "^CONFIG_UIO$"), r"CONFIG_UIO=y",
     ".config diff BR-0 vs official+multi-os: the only 3 symbols that change"),
    ("Which driver binds the GPU (arm,mali-bifrost)?", lambda: B.bsp_grep("mali", '"arm,mali-bifrost"', None, 5, V1),
     r"mali_kbase_core_linux\.c:\d+:", "tar -xzOf mali-g31_km_v1.3.0.tar.gz"),
    ("Which driver handles renesas,vcp4-*?", lambda: B.bsp_grep("uvcs", "vcp4-", None, 5, V1), r"uvcs_lkm_internal\.h:120:",
     "tar -xjOf uvcs_kernel_package_v5.1.5.0.tar.bz2"),
    ("Which driver handles renesas,mmngr?", lambda: B.bsp_grep("mmngr", '"renesas,mmngr"', None, 5, V1), r"mmngr_drv\.c:2005:",
     "git grep in the v1 DL_DIR mirror mmngr_drv @0b52d57"),
    ("U-Boot defconfig", lambda: B.bsp_recipe("u-boot", "^UBOOT_CONFIG$", V1), r'smarc-rzg3l\.conf:27',
     "meta-renesas machine conf: UBOOT_CONFIG[smarc-rzg3l] = smarc-rzg3l_defconfig (catalog)"),
    ("Kernel defconfig and device tree", lambda: B.bsp_recipe("linux-renesas", "^(KBUILD_DEFCONFIG|KERNEL_DEVICETREE)$", V1),
     r'(?s)KBUILD_DEFCONFIG="defconfig".*KERNEL_DEVICETREE="renesas/r9a08g046l48-smarc\.dtb"', "bitbake -e (scratch build)"),
]
for q, call, want, src in qa:
    try:
        out = call()
    except Exception as ex:
        out = f"ERROR {ex}"
    ok = re.search(want, out) is not None
    check(ok, f"Q: {q}  [{src}]" + ("" if ok else f"\n      got: {out[:300]!r}"))

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
