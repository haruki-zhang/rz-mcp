"""PoC lifecycle tests (no hardware, no Yocto build): poc_create, poc_status at every step of the flow, and
poc_package of the past delivery rzg3l-lowpower-display (copied, never written to).

    .venv/bin/python eval/test_poc.py [--keep]

Works under a temporary PoC root selected through RZ_MCP_CONFIG; build / flash / verify results are faked with
poc.record(). The packaging job runs for real (md2pdf needs firefox and gs). Removes the jobs it created from
data/jobs, and the temporary PoC root unless --keep.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("/home/mpu-epmaj/max/workspace/tmp-p3p4/p4-tests")
PAST = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1/rzg3l-lowpower-display")
POCS = BASE / "pocs"
shutil.rmtree(POCS, ignore_errors=True)
POCS.mkdir(parents=True)
(BASE / "config.toml").write_text(f'[yocto]\npackages = "{ROOT / "packages"}"\n\n[poc]\nroot = "{POCS}"\n')
os.environ["RZ_MCP_CONFIG"] = str(BASE / "config.toml")
sys.path.insert(0, str(ROOT / "src"))
from rzmcp import jobs, poc  # noqa: E402

created_jobs: list[str] = []
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"))


def step(name: str, n: int, tool, env="usable", contains: str = "") -> dict:
    a = poc.assess(name, env=env)
    ok = a["step"] == n and a["tool"] == tool and contains in a["next"]
    check(f"status {name}: step {n} -> {tool}" + (f" ({contains})" if contains else ""), ok,
          f"got step {a['step']} tool {a['tool']}: {a['next']}")
    return a


def edit(p: Path, text: str) -> None:
    p.write_text(p.read_text(encoding="utf-8") + text, encoding="utf-8")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_job(text: str, timeout: float = 600) -> dict:
    m = re.search(r"已启动作业 (\S+)：", text)
    assert m, text
    created_jobs.append(m.group(1))
    deadline = time.time() + timeout
    while True:
        st = jobs.wait(m.group(1), 50)
        if st["state"] in jobs.TERMINAL or time.time() > deadline:
            return st


def tree(d: Path, skip=(".git",)) -> dict[str, str]:
    return {p.relative_to(d).as_posix(): p for p in d.rglob("*")
            if p.is_file() and not set(p.relative_to(d).parts) & set(skip)}


# ---------------------------------------------------------------- poc_create

def test_create():
    out = poc.create("wdt-reboot", "Reboot the board from the watchdog")
    d = POCS / "wdt-reboot"
    expect = ["README.md", "docs/RZG3L-Wdt-Reboot-Guide.md", "image/README.md", "tools/md2pdf.py", "build.toml",
              "design/design.md", "design/verify.toml", ".rz-poc/state.json", ".gitignore",
              "layers/meta-rzg3l-wdt-reboot/conf/layer.conf", "layers/meta-rzg3l-wdt-reboot/README",
              "layers/meta-rzg3l-wdt-reboot/COPYING.MIT"]
    missing = [e for e in expect if not (d / e).is_file()]
    check("create: layout", not missing, f"missing {missing}")
    lc = (d / "layers/meta-rzg3l-wdt-reboot/conf/layer.conf").read_text()
    check("create: layer.conf valid", 'LAYERSERIES_COMPAT_meta-rzg3l-wdt-reboot = "scarthgap"' in lc
          and 'BBFILE_COLLECTIONS += "meta-rzg3l-wdt-reboot"' in lc and "{" + "LAYER}" not in lc, lc)
    bt = (d / "build.toml").read_text()
    import tomllib
    b = tomllib.loads(bt)
    check("create: build.toml", b["image"] == "core-image-minimal" and b["components"] == []
          and b["layers"] == ["layers/meta-rzg3l-wdt-reboot"] and "local_conf" in b, bt)
    check("create: design status line", (d / "design/design.md").read_text().startswith("状态：草稿"))
    check("create: no placeholders left", not any(re.search(r"\{(TITLE|GUIDE|NAME|LAYER|SUMMARY)\}", p.read_text(errors="ignore"))
                                                  for p in d.rglob("*") if p.is_file() and ".git" not in p.parts
                                                  and p.suffix != ".py"))
    check("create: md2pdf copied as is", sha(d / "tools/md2pdf.py") == sha(PAST / "tools/md2pdf.py"))
    gi = (d / ".gitignore").read_text()
    check("create: .gitignore", all(x in gi for x in ("image/*.wic.gz", "dist/", ".rz-poc/build/")), gi)
    st = poc.load_state("wdt-reboot")
    t = st.get("created", {}).get("templates", {})
    check("create: template hashes recorded", all(k in t for k in (
        "design/design.md", "design/verify.toml", "README.md", "docs/RZG3L-Wdt-Reboot-Guide.md", "image/README.md")), t)
    log = poc._git(d, "log", "--oneline")
    check("create: git commit", log is not None and log.returncode == 0 and "poc_create" in log.stdout,
          log and log.stderr)
    check("create: answer names next steps", all(x in out for x in ("yocto_status", "design.md", "verify.toml",
                                                                      "已确认")), out)
    for bad in ("Bad", "a_b", "", "-x", "x-", "a--b", "../etc"):
        try:
            poc.create(bad, "x")
            check(f"create: invalid name {bad!r} refused", False)
        except poc.PocError as e:
            check(f"create: invalid name {bad!r} refused", "不合法" in str(e), str(e))
    try:
        poc.create("wdt-reboot", "again")
        check("create: duplicate refused", False)
    except poc.PocError as e:
        check("create: duplicate refused", "poc_status" in str(e), str(e))


# ---------------------------------------------------------------- poc_status

def fake_build(name: str, rootfs: str, fip: str = "f1", ok: bool = True, **kw) -> None:
    if not ok:
        poc.record(name, "build", {"ok": False, "error": "do_compile failed in linux-renesas", "log": "/tmp/x.log",
                                   **kw})
        return
    poc.record(name, "build", {"ok": True, "job": "j-build", "image": "core-image-minimal", "dir": "/x",
                               "files": {"rootfs": "/x/r.wic.gz", "fip_bin": "/x/fip.bin"},
                               "sha256": {"rootfs": rootfs, "fip_bin": fip}, "inputs_sha256": poc.inputs_sha256(name),
                               **kw})


def test_status_flow():
    n = "flow-demo"
    poc.create(n, "Status flow")
    d = POCS / n
    step(n, 0, "yocto_status", env="missing")
    step(n, 1, None, contains="design/design.md")
    # confirming a template is not writing it
    (d / "design/design.md").write_text((d / "design/design.md").read_text().replace("状态：草稿", "状态：已确认 2026-11-27", 1))
    step(n, 1, None, contains="design/design.md")
    (d / "design/design.md").write_text((d / "design/design.md").read_text().replace("状态：已确认 2026-11-27", "状态：草稿", 1))
    edit(d / "design/design.md", "\n方案：写好了。\n")
    step(n, 1, None, contains="verify.toml")
    edit(d / "design/verify.toml", '\n[[check]]\nid = "V-3"\ntitle = "wdt"\nwhere = "linux"\nrun = "ls"\nexpect = "x"\n')
    step(n, 2, None, contains="已确认")
    text = (d / "design/design.md").read_text().replace("状态：草稿", "状态：已确认 2026-11-27", 1)
    (d / "design/design.md").write_text(text)
    step(n, 3, "poc_build", contains="还没有构建")
    step(n, 0, "yocto_status", env="broken")
    fake_build(n, "r0", ok=False)
    step(n, 3, "poc_build", contains="do_compile failed")
    fake_build(n, "r1")
    step(n, 4, "board_flash", contains='parts="all"')
    step(n, 4, "board_flash", env="broken")                 # environment only matters while a build is needed
    edit(d / "layers/meta-rzg3l-flow-demo/conf/layer.conf", '\nIMAGE_INSTALL:append = " wdt-test"\n')
    step(n, 3, "poc_build", contains="改过了")
    fake_build(n, "r2")
    poc.record(n, "flash", {"ok": True, "job": "j-f1", "parts": "all", "boot_device": "emmc",
                            "sha256": {"rootfs": "r1", "fip_bin": "f1"}, "time": "2026-11-27T10:00:00"})
    step(n, 4, "board_flash", contains='parts="rootfs"')     # older build on the board, same bootloader
    poc.record(n, "flash", {"ok": False, "job": "j-f2", "error": "TFTP timeout", "time": "2026-11-27T10:05:00"})
    step(n, 4, "board_flash", contains="TFTP timeout")
    poc.record(n, "verify", {"ok": True, "job": "j-v0", "report": "/x/old.md", "passed": 2, "failed": 0,
                             "time": "2026-11-27T10:06:00"})
    poc.record(n, "flash", {"ok": True, "job": "j-f3", "parts": "rootfs", "boot_device": "emmc",   # fip carried over
                            "sha256": {"rootfs": "r2", "fip_bin": "f1"}, "time": "2026-11-27T10:10:00"})
    step(n, 4, "board_verify", contains=f'plan="{n}"')      # the only verify is older than the flash
    poc.record(n, "verify", {"ok": False, "job": "j-v1", "error": "serial busy", "time": "2026-11-27T10:20:00"})
    step(n, 4, "board_verify", contains="serial busy")
    poc.record(n, "verify", {"ok": False, "job": "j-v2", "report": "/x/report-v2.md", "passed": 1, "failed": 1,
                             "time": "2026-11-27T10:30:00"})
    step(n, 3, "poc_build", contains="/x/report-v2.md")
    edit(d / "layers/meta-rzg3l-flow-demo/conf/layer.conf", "\n# fix\n")
    step(n, 3, "poc_build", contains="改过了")
    fake_build(n, "r3", time="2026-11-27T10:40:00")
    step(n, 4, "board_flash", contains='parts="rootfs"')
    poc.record(n, "flash", {"ok": True, "job": "j-f4", "parts": "rootfs", "boot_device": "emmc",
                            "sha256": {"rootfs": "r3"}, "time": "2026-11-27T10:50:00"})
    step(n, 4, "board_verify")
    poc.record(n, "verify", {"ok": True, "job": "j-v3", "report": "/x/report-v3.md", "passed": 2, "failed": 0,
                             "time": "2026-11-27T11:00:00"})
    a = step(n, 5, None, contains="README.md")
    check("status: docs list names the guide", "RZG3L-Flow-Demo-Guide.md" in a["next"], a["next"])
    for rel in ("README.md", "docs/RZG3L-Flow-Demo-Guide.md"):
        edit(d / rel, "\nWritten.\n")
    step(n, 5, None, contains="image/README.md")
    edit(d / "image/README.md", "\nWritten.\n")
    step(n, 5, "poc_package")
    poc.record(n, "package", {"ok": True, "job": "j-p", "dir": "/x/dist", "version": "v1.0", "image_sha256": "r2"})
    step(n, 5, "poc_package")                                # package of an older image
    poc.record(n, "package", {"ok": True, "job": "j-p", "dir": "/x/dist", "version": "v1.0", "image_sha256": "r3"})
    a = step(n, 5, None, contains="已完成")
    check("status: done flag", a["done"])
    txt = poc.status_text(n)
    check("status text: step, results, next", all(x in txt for x in ("第 5 步", "上一次构建", "上一次写板", "上一次验证",
                                                                    "上一次打包", "下一步")), txt)
    lst = poc.status_text()
    check("status list: both PoCs", "flow-demo" in lst and "wdt-reboot" in lst, lst)
    try:
        poc.status_text("no-such-poc")
        check("status: unknown PoC", False)
    except poc.PocError as e:
        check("status: unknown PoC", "poc_create" in str(e), str(e))


# ---------------------------------------------------------------- poc_package

def test_package_refusals():
    out = poc.package_start("wdt-reboot", "v1.0")
    check("package: skeleton refused", "还不能打包" in out and "README.md" in out and "layers/" in out
          and "poc_build" in out, out)
    check("package: skeleton wrote nothing", not (POCS / "wdt-reboot" / "dist").exists())
    try:
        poc.package_start("wdt-reboot", "v 1")
        check("package: bad version refused", False)
    except poc.PocError:
        check("package: bad version refused", True)


def setup_past() -> tuple[str, Path]:
    n = "lowpower-display"
    d = POCS / n
    shutil.copytree(PAST, d, symlinks=True)
    bdir = d / ".rz-poc" / "build"
    bdir.mkdir(parents=True)
    img = "lpdemo-image-smarc-rzg3l.rootfs"
    os.replace(d / "image" / f"{img}.wic.gz", bdir / f"{img}.wic.gz")
    os.replace(d / "image" / f"{img}.wic.bmap", bdir / f"{img}.wic.bmap")
    (bdir / "fip-smarc-rzg3l.bin").write_bytes(b"fake fip for the test\n" * 10)
    (d / "build.toml").write_text('image = "lpdemo-image"\ncomponents = ["multi-os"]\nlayers = '
                                  '["layers/meta-rzg3l-seamless-logo-still", "layers/meta-rzg3l-lp-display"]\n'
                                  'local_conf = """\nUSE_OPENAMP = "0"\n"""\n')
    (d / "design").mkdir()
    (d / "design" / "design.md").write_text("状态：已确认 2026-11-19\n# LowPower Display：设计\n")
    (d / "design" / "verify.toml").write_text('[[check]]\nid = "V-1"\ntitle = "k"\nwhere = "linux"\nrun = "uname -r"\n'
                                              'expect = "6"\n')
    files = {"rootfs": bdir / f"{img}.wic.gz", "bmap": bdir / f"{img}.wic.bmap", "fip_bin": bdir / "fip-smarc-rzg3l.bin"}
    poc.record(n, "build", {"ok": True, "job": "j-past", "image": "lpdemo-image", "dir": str(bdir),
                            "files": {k: str(v) for k, v in files.items()}, "sha256": {k: sha(v) for k, v in files.items()},
                            "inputs_sha256": poc.inputs_sha256(n), "conf_sha256": "c0",
                            "layers": [{"path": "meta-renesas", "git": "abc"}], "seconds": 1})
    return n, d


def test_package_past():
    n, d = setup_past()
    t0 = time.time()
    st = run_job(poc.package_start(n, "v1.0"))
    check("package past: job succeeded", st["state"] == "succeeded", f"{st.get('message')} {st.get('hint')}")
    out = d / "dist" / f"{n}-v1.0"
    want = set(tree(PAST, skip=(".git", ".gitignore"))) | {"versions.json", "SHA256SUMS"}
    got = tree(out)
    check("package past: file tree equals the delivery", set(got) == want,
          f"extra {set(got) - want}, missing {want - set(got)}")
    orig = tree(PAST, skip=(".git", ".gitignore"))
    differ = [r for r in orig if r in got and not r.endswith(".pdf") and sha(orig[r]) != sha(got[r])]
    check("package past: contents equal the delivery (except the re-rendered PDF)", not differ, differ)
    pdf = out / "docs" / "RZG3L-LowPower-Display-Guide.pdf"
    check("package past: PDF rendered now", pdf.is_file() and pdf.read_bytes()[:5] == b"%PDF-"
          and pdf.stat().st_mtime >= t0)
    sums = dict(reversed(line.split("  ", 1)) for line in (out / "SHA256SUMS").read_text().splitlines())
    check("package past: SHA256SUMS covers and matches every file",
          set(sums) == set(got) - {"SHA256SUMS"} and all(sha(out / r) == s for r, s in sums.items()))
    v = json.loads((out / "versions.json").read_text())
    check("package past: versions.json", v["image"]["sha256"] == sha(out / "image/lpdemo-image-smarc-rzg3l.rootfs.wic.gz")
          and v["boot_files"]["fip_bin"]["file"] == "fip-smarc-rzg3l.bin" and v["poc"]["git"]
          and v["build"]["layers"] and "bsp" in v, v)
    check("package past: no internal files", not any(r.split("/")[0] in ("design", "build.toml", ".rz-poc", "dist")
                                                     for r in got))
    st_ = poc.load_state(n)
    check("package past: state recorded", st_.get("package", {}).get("ok") and st_["package"]["dir"] == str(out))
    a = poc.assess(n, env="usable")
    check("package past: poc_status wants a flash first", a["step"] == 4 and a["tool"] == "board_flash", a)
    rootfs = poc.load_state(n)["build"]["sha256"]["rootfs"]
    poc.record(n, "flash", {"ok": True, "job": "j", "parts": "all", "boot_device": "emmc", "sha256": {"rootfs": rootfs}})
    poc.record(n, "verify", {"ok": True, "job": "j", "report": "/x/r.md", "passed": 1, "failed": 0})
    a = poc.assess(n, env="usable")
    check("package past: poc_status says done", a["done"], a)

    # re-run replaces the previous output of the same version
    (out / "stale.txt").write_text("x")
    st = run_job(poc.package_start(n, "v1.0"))
    check("package re-run: replaces the output", st["state"] == "succeeded" and not (out / "stale.txt").exists()
          and not list((d / "dist").glob(".*")), st.get("message"))

    # licensed content: a package zip, a file from inside one, a meta-rz-features directory
    zips = sorted((ROOT / "packages").rglob("*.zip"))
    if zips:
        small = min(zips, key=lambda z: z.stat().st_size)
        shutil.copy2(small, d / "tools" / small.name)
        (d / "docs" / "meta-rz-features-x").mkdir()
        (d / "docs" / "meta-rz-features-x" / "note.txt").write_text("x")
        st = run_job(poc.package_start(n, "v2.0"))
        msg = st.get("message", "")
        check("package licensed: zip and meta-rz-features refused", st["state"] == "failed" and small.name in msg
              and "meta-rz-features-x" in msg and not (d / "dist" / f"{n}-v2.0").exists(), msg)
        check("package licensed: failure recorded", poc.load_state(n)["package"].get("ok") is False)
        os.remove(d / "tools" / small.name)
        shutil.rmtree(d / "docs" / "meta-rz-features-x")
        with zipfile.ZipFile(small) as z:
            m = max((i for i in z.infolist() if not i.is_dir()), key=lambda i: i.file_size)
            (d / "docs" / "inner.bin").write_bytes(z.read(m))
        st = run_job(poc.package_start(n, "v2.0"))
        check("package licensed: file from inside a zip refused", st["state"] == "failed"
              and "docs/inner.bin" in st.get("message", ""), st.get("message"))
        os.remove(d / "docs" / "inner.bin")
    else:
        check("package licensed: packages/ has zips to test with", False, str(ROOT / "packages"))

    # changed build file / changed layers after the build
    fip = d / ".rz-poc/build/fip-smarc-rzg3l.bin"
    fip.write_bytes(fip.read_bytes() + b"!")
    out2 = poc.package_start(n, "v3.0")
    check("package: changed build file refused", "还不能打包" in out2 and "fip_bin" in out2, out2)
    fip.write_bytes(fip.read_bytes()[:-1])
    edit(d / "layers/meta-rzg3l-lp-display/README", "\nchanged\n")
    out3 = poc.package_start(n, "v3.0")
    check("package: layers changed after the build refused", "还不能打包" in out3 and "改过了" in out3, out3)
    check("package: refusals wrote nothing", not (d / "dist" / f"{n}-v3.0").exists())


def main():
    try:
        test_create()
        test_status_flow()
        test_package_refusals()
        test_package_past()
    finally:
        for j in created_jobs:
            shutil.rmtree(jobs.JOBS_DIR / j, ignore_errors=True)
        if "--keep" not in sys.argv:
            shutil.rmtree(POCS, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
