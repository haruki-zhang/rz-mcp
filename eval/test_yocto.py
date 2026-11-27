"""Yocto tests (no hardware): yocto_status / yocto_setup / poc_build (src/rzmcp/yocto.py).

    .venv/bin/python eval/test_yocto.py            fast tests (seconds)
    .venv/bin/python eval/test_yocto.py --real     + the real run: yocto_setup into tmp-p3p4/yocto through the job
                                                   runner (cancel mid-way, re-run, reuse), then poc_build of a fake
                                                   PoC with cheap bitbake targets (parse; a cancelled fetch)

Everything is written under /home/mpu-epmaj/max/workspace/tmp-p3p4/ (a test config passed with RZ_MCP_CONFIG).
The fast tests that need real BSP layers use the ones of tmp-p3p4/yocto (symlinked) and are skipped without it.
"""
import contextlib
import fcntl
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

BASE = Path("/home/mpu-epmaj/max/workspace/tmp-p3p4")
T = BASE / "p3-tests"
REAL = BASE / "yocto"
ROOT = Path(__file__).resolve().parents[1]


def write_cfg(name: str, project: Path, packages: Path = ROOT / "packages") -> Path:
    f = T / f"{name}.toml"
    f.write_text(f'[yocto]\nproject = "{project}"\ncontainer = "max-ubuntu22.04"\ndefault_image = "core-image-minimal"\n'
                 f'packages = "{packages}"\n[poc]\nroot = "{T / "poc"}"\n')
    return f


T.mkdir(parents=True, exist_ok=True)
os.environ["RZ_MCP_CONFIG"] = str(write_cfg("real", REAL))        # job runners inherit it
sys.path.insert(0, str(ROOT / "src"))
from rzmcp import config, jobs, yocto  # noqa: E402
from rzmcp.jobs import JobError  # noqa: E402


class Ctx:
    """Stand-in for jobs.Ctx when a job function's helpers are called directly."""

    def __init__(self):
        self.dir, self.job, self.params, self.lines = T, {"id": "test"}, {}, []

    def log(self, line, key=False):
        self.lines.append(line)

    def progress(self, text):
        pass

    def check_cancel(self):
        pass

    def step(self, i, title=None):
        self.lines.append(f"== step {i}")

    def report(self, md):
        self.reported = md


@contextlib.contextmanager
def using(cfg_file: Path):
    """Point config.load() (and so the tool functions) at another config file in this process."""
    old, config.CONFIG_PATH = config.CONFIG_PATH, cfg_file
    try:
        yield
    finally:
        config.CONFIG_PATH = old


def cfg_for(project: Path, packages: Path = ROOT / "packages") -> config.Config:
    return config.load(write_cfg("t", project, packages))


def fresh(name: str) -> Path:
    d = T / name
    shutil.rmtree(d, ignore_errors=True)
    return d


def raises(fn, *needles) -> str:
    try:
        fn()
    except JobError as e:
        text = f"{e}\n{e.hint}"
        for n in needles:
            assert n in text, f"{n!r} not in {text!r}"
        return text
    raise AssertionError("no JobError")


def fake_project() -> Path:
    """A project made of symlinks to the real project's release layers (usable, without its own data)."""
    if not (REAL / "meta-renesas").is_dir():
        raise Skip(f"{REAL} has no layers yet (run with --real first)")
    d = fresh("fake-project")
    d.mkdir()
    for l in ("poky", "meta-arm", "meta-openembedded", "meta-renesas"):
        (d / l).symlink_to(REAL / l)
    return d


class Skip(Exception):
    pass


# ---------------------------------------------------------------- fast tests

def test_assess_states():
    assert yocto.assess(cfg_for(T / "nonexistent"))["state"] == "absent"
    d = fresh("empty")
    (d / ".vscode").mkdir(parents=True)
    assert yocto.assess(cfg_for(d))["state"] == "absent"
    f = fresh("foreign")
    (f / "src").mkdir(parents=True)
    (f / "README").write_text("something else")
    a = yocto.assess(cfg_for(f))
    assert a["state"] == "unusable" and "README" in a["problems"][0], a
    # an interrupted rz-mcp setup is incomplete, not unusable
    i = fresh("interrupted")
    (i / ".rz-mcp").mkdir(parents=True)
    (i / ".rz-mcp" / "project.json").write_text(json.dumps({"created_by": "rz-mcp", "done": None}))
    (i / "poky").mkdir()
    assert yocto.assess(cfg_for(i))["state"] == "incomplete"
    # tool text: unusable is refused without a job and without touching it
    before = sorted(p.name for p in f.iterdir())
    with using(write_cfg("t", f)):
        out = yocto.tool_setup(["graphics"])
        assert out.startswith("没有启动") and "不可用" in out, out
        assert "unusable" not in yocto.tool_status() and "不可用" in yocto.tool_status()
    assert sorted(p.name for p in f.iterdir()) == before


def test_assess_usable():
    d = fake_project()
    a = yocto.assess(cfg_for(d))
    assert a["state"] == "usable", a
    # a different release is unusable: a meta-renesas whose kernel pin differs
    bad = fresh("other-release")
    bad.mkdir()
    for l in ("poky", "meta-arm", "meta-openembedded"):
        (bad / l).symlink_to(REAL / l)
    shutil.copytree(REAL / "meta-renesas", bad / "meta-renesas", ignore=shutil.ignore_patterns(".git"), symlinks=True)
    r = bad / "meta-renesas" / yocto.release()["components"]["kernel"]["recipe"]
    r.write_text(r.read_text().replace(yocto.release()["components"]["kernel"]["rev"], "0" * 40))
    a = yocto.assess(cfg_for(bad))
    assert a["state"] == "unusable" and any("kernel" in p for p in a["problems"]), a


def test_components():
    d = fake_project()
    cfg = cfg_for(d)
    ctx = Ctx()
    msg = yocto.install_component(ctx, cfg, d, "codecs")          # nested zip, small
    assert "已安装 5.1.5.0" in msg and (d / "meta-rz-features/meta-rz-codecs/conf/layer.conf").exists()
    assert yocto.install_component(ctx, cfg, d, "codecs") == "codecs: 已安装"
    yocto.install_component(ctx, cfg, d, "security")
    st = json.loads((d / ".rz-mcp/project.json").read_text())
    assert st["components"]["security"]["version"] == "6.12-v1.0"
    a = yocto.assess(cfg)
    assert a["components"]["codecs"]["version"] == "5.1.5.0" and "graphics" not in a["components"]
    assert not list((d / ".rz-mcp" / "stage").iterdir())
    # wrong sha256: refused, nothing unpacked
    pk = fresh("packages")
    c = yocto.component("graphics")
    (pk / Path(c["file"]).parent).mkdir(parents=True)
    with zipfile.ZipFile(pk / c["file"], "w") as z:
        z.writestr("x.txt", "not the package")
    raises(lambda: yocto.install_component(ctx, cfg_for(d, pk), d, "graphics"), "sha256", c["sha256"])
    assert not (d / "meta-rz-features/meta-rz-graphics").exists()
    # missing package: names the file and the directory to put it in
    (pk / c["file"]).unlink()
    raises(lambda: yocto.install_component(ctx, cfg_for(d, pk), d, "graphics"),
           "RTK0EF0045Z14001ZJ-5.1.3.2_JP.zip", str(pk / "RZG3L-BSP-1.0.0"))
    # the setup tool refuses before starting a job
    with using(write_cfg("t", d, pk)):
        out = yocto.tool_setup(["graphics"])
        assert out.startswith("没有启动") and "RTK0EF0045Z14001ZJ-5.1.3.2_JP.zip" in out, out
    raises(lambda: yocto.component("gpu"), "graphics")


def fake_poc(name="fake-poc", layer_ok=True) -> Path:
    d = fresh(f"poc/{name}")
    (d / "layers/meta-rzg3l-fake/conf").mkdir(parents=True)
    if layer_ok:
        (d / "layers/meta-rzg3l-fake/conf/layer.conf").write_text(
            'BBPATH .= ":${LAYERDIR}"\nBBFILES += "${LAYERDIR}/recipes-*/*/*.bb ${LAYERDIR}/recipes-*/*/*.bbappend"\n'
            'BBFILE_COLLECTIONS += "rzg3l-fake"\nBBFILE_PATTERN_rzg3l-fake = "^${LAYERDIR}/"\n'
            'BBFILE_PRIORITY_rzg3l-fake = "10"\nLAYERSERIES_COMPAT_rzg3l-fake = "scarthgap"\n')
    (d / "build.toml").write_text('image = "core-image-minimal"\ncomponents = ["codecs"]\n'
                                  'layers = ["layers/meta-rzg3l-fake"]\nlocal_conf = """\nIMAGE_INSTALL:append = " htop"\n"""\n')
    return d


def test_conf_generation():
    d = fake_project()
    cfg = cfg_for(d)
    meta = yocto.write_conf(d, by="yocto_setup")
    conf = d / "build/conf"
    bbl, local = (conf / "bblayers.conf").read_text(), (conf / "local.conf").read_text()
    assert "${TOPDIR}/../meta-renesas/meta-rz-bsp" in bbl and "BBLAYERS +=" not in bbl
    assert all(s in local for s in yocto.STANDARD) and 'DISTRO ?= "rz-vlp"' in local
    ci = yocto.conf_info(d)
    assert ci["config"] == "baseline" and not ci["modified"] and ci["conf_sha256"] == meta["conf_sha256"]
    assert not (conf / "backup").exists()
    p = fake_poc()
    spec = yocto.read_build_toml("fake-poc", cfg)
    assert spec["image"] == "core-image-minimal" and spec["layers"] == [p / "layers/meta-rzg3l-fake"]
    layers = [d / "meta-rz-features/meta-rz-codecs"] + spec["layers"]
    meta2 = yocto.write_conf(d, "fake-poc", layers, spec["local_conf"])
    bbl = (conf / "bblayers.conf").read_text()
    assert f"  {p / 'layers/meta-rzg3l-fake'} \\" in bbl and "meta-rz-codecs" in bbl
    assert 'IMAGE_INSTALL:append = " htop"' in (conf / "local.conf").read_text()
    assert yocto.conf_info(d)["config"] == "PoC fake-poc" and meta2["conf_sha256"] != meta["conf_sha256"]
    backups = list((conf / "backup").iterdir())
    assert len(backups) == 1 and "baseline" in (backups[0] / "rz-mcp.json").read_text()
    with open(conf / "local.conf", "a") as f:
        f.write('BB_NUMBER_THREADS = "4"\n')
    assert yocto.conf_info(d)["modified"]
    with using(write_cfg("t", d)):
        assert "PoC fake-poc（poc_build 写于" in yocto.tool_status() and "手动改过" in yocto.tool_status()
    # a layer that is not a layer, and a broken build.toml
    fake_poc("bad-poc", layer_ok=False)
    raises(lambda: yocto.read_build_toml("bad-poc", cfg), "layers/meta-rzg3l-fake", "layer.conf")
    (T / "poc/bad-poc/build.toml").write_text("image = ")
    raises(lambda: yocto.read_build_toml("bad-poc", cfg), "TOML")


def test_bitbake_busy():
    d = fresh("busy")
    (d / "build").mkdir(parents=True)
    assert not yocto.bitbake_busy(d / "build")
    with open(d / "build/bitbake.lock", "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)        # what bitbake's server does
        assert yocto.bitbake_busy(d / "build")
        assert yocto.running(cfg_for(d))["bitbake"]
        old, yocto.BITBAKE_WAIT = yocto.BITBAKE_WAIT, 2
        try:
            raises(lambda: yocto.run_bitbake(Ctx(), cfg_for(d), ["-p"]), "bitbake 正在", str(d / "build"))
        finally:
            yocto.BITBAKE_WAIT = old
    assert not yocto.bitbake_busy(d / "build")


def test_collect():
    d = fresh("deploy-project")
    dep = d / "build" / yocto.DEPLOY
    dep.mkdir(parents=True)
    img = "core-image-minimal"
    for ts in ("20261101000000", "20261127000000"):
        (dep / f"{img}-smarc-rzg3l.rootfs-{ts}.wic.gz").write_bytes(ts.encode())
        (dep / f"{img}-smarc-rzg3l.rootfs-{ts}.wic.bmap").write_text(ts)
    (dep / f"{img}-smarc-rzg3l.rootfs.wic.gz").symlink_to(f"{img}-smarc-rzg3l.rootfs-20261127000000.wic.gz")
    (dep / f"{img}-smarc-rzg3l.rootfs.wic.bmap").symlink_to(f"{img}-smarc-rzg3l.rootfs-20261127000000.wic.bmap")
    for n in ("bl2_bp_mmc-smarc-rzg3l.srec", "bl2_bp_spi-smarc-rzg3l.srec", "fip-smarc-rzg3l.srec", "fip-g3l.bin"):
        (dep / n).write_text(n)
    (dep / "fip-smarc-rzg3l.bin").symlink_to("fip-g3l.bin")
    dir_, files, sha = yocto.collect(d, img)
    assert dir_ == dep and files["rootfs"].endswith("rootfs-20261127000000.wic.gz") and files["fip_bin"].endswith("fip-g3l.bin")
    assert set(files) == {"rootfs", "bmap", "bl2_emmc", "bl2_qspi", "fip", "fip_bin"}, files
    dest = fresh("poc-copy") / ".rz-poc" / "build"
    dest.mkdir(parents=True)
    (dest / "stale.wic.gz").write_text("old")
    dir_, files, sha = yocto.collect(d, img, dest)
    assert dir_ == dest and not (dest / "stale.wic.gz").exists()
    assert files["rootfs"] == str(dest / f"{img}-smarc-rzg3l.rootfs.wic.gz") and Path(files["rootfs"]).read_bytes() == b"20261127000000"
    assert not Path(files["rootfs"]).is_symlink() and sha["bl2_emmc"] == yocto._sha256(dep / "bl2_bp_mmc-smarc-rzg3l.srec")
    raises(lambda: yocto.collect(d, "core-image-weston"), "core-image-weston-smarc-rzg3l.rootfs.wic.gz")
    # the record round trip that board_flash reads
    cfg = cfg_for(d)
    yocto._save_record(cfg, {"ok": True, "job": "x", "poc": None, "image": img, "dir": str(dep), "files": files})
    assert yocto.last_build(cfg)["files"]["rootfs"] == files["rootfs"]


def test_job_lock():
    with jobs.hold(yocto.LOCK, "作业 20261127-000000-poc_build-test（测试）"):
        r = yocto.running()
        assert r["job"]["id"] == "20261127-000000-poc_build-test", r
        out = yocto.tool_build()
        if (REAL / "meta-renesas").is_dir():         # with an absent project tool_build refuses earlier
            assert out.startswith("没有启动") and "20261127-000000-poc_build-test" in out, out
        out = yocto.tool_setup()
        assert out.startswith("没有启动") and "20261127-000000-poc_build-test" in out, out
        assert "job_status(job_id=\"20261127-000000-poc_build-test\"" in yocto.tool_status()
    assert yocto.running()["job"] is None


def test_build_job_offline():
    """_build_job end to end with bitbake replaced: components, conf, collection, the two records."""
    d = fake_project()
    p = fake_poc()
    cfg_file = write_cfg("t", d)
    dep = d / "build" / yocto.DEPLOY
    dep.mkdir(parents=True)
    (dep / "core-image-minimal-smarc-rzg3l.rootfs-20261127010101.wic.gz").write_bytes(b"wic")
    (dep / "core-image-minimal-smarc-rzg3l.rootfs.wic.gz").symlink_to("core-image-minimal-smarc-rzg3l.rootfs-20261127010101.wic.gz")
    (dep / "fip-smarc-rzg3l.srec").write_text("fip")
    calls = []
    real_run = yocto.run_bitbake
    yocto.run_bitbake = lambda ctx, cfg, args: calls.append(args) or {"rc": 0, "errors": [], "failed": [], "logs": []}
    ctx = Ctx()
    ctx.params = {"poc": "fake-poc", "image": None}
    try:
        with using(cfg_file):
            from rzmcp import poc as pocmod
            want_inputs = pocmod.inputs_sha256("fake-poc", config.load())
            out = yocto._build_job(ctx)
    finally:
        yocto.run_bitbake = real_run
    assert calls == [["core-image-minimal"]], calls
    rec = out["record"]
    assert rec["ok"] and rec["poc"] == "fake-poc" and rec["inputs_sha256"] == want_inputs
    assert rec["files"]["rootfs"] == str(p / ".rz-poc/build/core-image-minimal-smarc-rzg3l.rootfs.wic.gz")
    assert set(rec["files"]) == {"rootfs", "fip"} and rec["sha256"]["fip"] == yocto._sha256(dep / "fip-smarc-rzg3l.srec")
    assert rec["conf_sha256"] == yocto.conf_info(d)["conf_sha256"]
    paths = [l["path"] for l in rec["layers"]]
    assert str(d / "meta-renesas/meta-rz-bsp") in paths and str(p / "layers/meta-rzg3l-fake") in paths
    assert next(l for l in rec["layers"] if l["path"].endswith("meta-rz-bsp"))["git"] == yocto.release()["layers"][3]["rev"]
    assert json.loads((p / ".rz-poc/state.json").read_text())["build"]["job"] == "test"
    assert json.loads((d / ".rz-mcp/last-build.json").read_text())["files"] == rec["files"]
    assert 'board_flash(poc="fake-poc"' in out["next"]
    # a failing bitbake: JobError with the failed task, both records say so
    yocto.run_bitbake = lambda ctx, cfg, args: {"rc": 1, "failed": ["/x/foo.bb:do_compile"], "logs": ["/x/log.do_compile.1"],
                                                "errors": ["ERROR: Task (/x/foo.bb:do_compile) failed with exit code '1'"]}
    try:
        with using(cfg_file):
            raises(lambda: yocto._build_job(ctx), "foo.bb:do_compile", "/x/log.do_compile.1", "poc_build")
    finally:
        yocto.run_bitbake = real_run
    lb = json.loads((d / ".rz-mcp/last-build.json").read_text())
    assert lb["ok"] is False and lb["log"] and json.loads((p / ".rz-poc/state.json").read_text())["build"]["ok"] is False
    assert (p / ".rz-poc/build/core-image-minimal-smarc-rzg3l.rootfs.wic.gz").exists()   # the last good copy stays


FAST = [test_assess_states, test_assess_usable, test_components, test_conf_generation, test_bitbake_busy,
        test_collect, test_build_job_offline, test_job_lock]


# ---------------------------------------------------------------- the real run (minutes; network)

def until_done(job_id: str, timeout: float, pred=None) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        st = jobs.wait(job_id, 20)
        if st["state"] in jobs.TERMINAL or (pred and pred(st)):
            return st
    raise AssertionError(f"{job_id} still {st['state']} after {timeout}s")


def job_id(text: str) -> str:
    assert text.startswith("已启动作业"), text
    return text.split()[0].removeprefix("已启动作业")


def test_real_setup():
    t0 = time.time()
    a = yocto.assess()
    if a["state"] == "absent":
        REAL.mkdir(parents=True, exist_ok=True)
        j = job_id(yocto.tool_setup(["graphics"]))
        st = until_done(j, 1800, lambda s: s.get("step") == 5 and "task" in (s.get("progress") or ""))
        print(f"     first run reached bitbake after {time.time() - t0:.0f}s; cancelling")
        st = jobs.cancel(j)
        assert st["state"] == "cancelled", st
        time.sleep(3)
        assert yocto.assess()["state"] == "incomplete"
    t1 = time.time()
    out = yocto.tool_setup(["graphics"])
    if out.startswith("已启动作业"):
        st = until_done(job_id(out), 3600)
        assert st["state"] == "succeeded", jobs.format_status(st)
        print(f"     re-run finished in {time.time() - t1:.0f}s")
    a = yocto.assess()
    assert a["state"] == "usable" and a["components"]["graphics"]["version"] == "5.1.3.2", a
    assert all(yocto.sources(REAL).values()), yocto.sources(REAL)
    t2 = time.time()
    out = yocto.tool_setup(["graphics"])                       # reuse: nothing left to do, no job
    assert "已经可用" in out and time.time() - t2 < 10, out
    print(f"     reuse answered in {time.time() - t2:.1f}s")


def test_real_build():
    fake_poc()
    title = "构建（测试）"
    # a cancelled bitbake stops in the container; the next build waits for bitbake.lock instead of failing
    st = jobs.start("poc_build", {"poc": "fake-poc", "image": "core-image-minimal",
                                  "bitbake_args": ["--runall=fetch", "core-image-minimal"]},
                    title=title, steps=yocto.BUILD_STEPS, poc="fake-poc", lock=yocto.LOCK)
    st = until_done(st["id"], 900, lambda s: s.get("step") == 4 and "task" in (s.get("progress") or ""))
    assert st["state"] == "running", jobs.format_status(st)
    out = yocto.tool_build("fake-poc")                         # Y4: refused while it runs
    assert out.startswith("没有启动") and st["id"] in out, out
    t = time.time()
    st = jobs.cancel(st["id"])
    assert st["state"] == "cancelled" and time.time() - t < 12, st
    rec = yocto.last_build()
    assert rec["ok"] is False and rec["poc"] == "fake-poc" and rec["error"] == "作业被取消", rec
    t = time.time()
    st = jobs.start("poc_build", {"poc": "fake-poc", "image": "core-image-minimal", "bitbake_args": ["-p"]},
                    title=title, steps=yocto.BUILD_STEPS, poc="fake-poc", lock=yocto.LOCK)
    st = until_done(st["id"], 900)
    assert st["state"] == "succeeded", jobs.format_status(st)
    print(f"     parse with the PoC layer and codecs: {time.time() - t:.0f}s")
    bbl = (REAL / "build/conf/bblayers.conf").read_text()
    assert "meta-rzg3l-fake" in bbl and "meta-rz-codecs" in bbl
    assert yocto.conf_info(REAL)["config"] == "PoC fake-poc"
    import subprocess
    ps = subprocess.run(["docker", "exec", "max-ubuntu22.04", "pgrep", "-f", "bitbake"], capture_output=True, text=True)
    print(f"     bitbake processes left in the container: {ps.stdout.split() or 'none'}")
    yocto.write_conf(REAL, by="yocto_setup")                  # leave the baseline for the next real build
    assert yocto.conf_info(REAL)["config"] == "baseline"


def main():
    tests = FAST + ([test_real_setup, test_real_build] if "--real" in sys.argv else [])
    failed = 0
    for t in tests:
        t0 = time.time()
        try:
            t()
            print(f"ok   {t.__name__} ({time.time() - t0:.1f}s)")
        except Skip as e:
            print(f"skip {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
