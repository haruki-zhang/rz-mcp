"""The shared Yocto project (config.toml yocto.project) of RZG3L-BSP-1.0.0: yocto_status, yocto_setup, poc_build.

    <project>/poky, meta-arm, meta-openembedded, meta-renesas   the release's layers (catalog/bsp_releases.toml)
    <project>/meta-rz-features/...                           optional components (graphics, codecs, security, multi-os)
    <project>/build/conf/                                    written by rz-mcp: rz-mcp.json says for whom (baseline / PoC)
    <project>/.rz-mcp/project.json                           what yocto_setup did (components, done)
    <project>/.rz-mcp/last-build.json                        record of the last poc_build (board_flash's default image)

bitbake runs in the build container (Ubuntu 22.04), which bind-mounts the project at the same path. yocto_setup and
poc_build hold the job lock "yocto", so there is never more than one bitbake.
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path

from . import ROOT, config, jobs
from . import poc as pocmod
from .jobs import JobError

RELEASE = "RZG3L-BSP-1.0.0"
CATALOG = ROOT / "catalog" / "bsp_releases.toml"
MACHINE = "smarc-rzg3l"
STATE_DIR = ".rz-mcp"
LOCK = "yocto"                                        # jobs lock of every job that runs bitbake
IGNORABLE = {".claude", ".vscode", STATE_DIR, "lost+found"}   # do not make a directory "not empty"
STANDARD = [f'MACHINE = "{MACHINE}"', 'RM_WORK_EXCLUDE += "linux-renesas u-boot trusted-firmware-a"']
DEPLOY = f"tmp/deploy/images/{MACHINE}"               # relative to <project>/build
MIN_FREE_GB, WARN_FREE_GB = 30, 100                   # one image build takes about 60 GB
BITBAKE_WAIT = 120                                    # seconds poc_build waits for a previous bitbake to exit
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
# key -> file name in the deploy directory (and in <poc>/.rz-poc/build/); {image} = the image recipe
BUILD_FILES = {
    "rootfs": "{image}-smarc-rzg3l.rootfs.wic.gz",
    "bmap": "{image}-smarc-rzg3l.rootfs.wic.bmap",
    "bl2_emmc": "bl2_bp_mmc-smarc-rzg3l.srec",
    "bl2_emmc_bin": "bl2_bp_mmc-smarc-rzg3l.bin",
    "bl2_qspi": "bl2_bp_spi-smarc-rzg3l.srec",
    "bl2_qspi_bin": "bl2_bp_spi-smarc-rzg3l.bin",
    "fip": "fip-smarc-rzg3l.srec",
    "fip_bin": "fip-smarc-rzg3l.bin",
    "flash_writer": "Flash_Writer_SCIF_RZG3L_SMARC_LPDDR4.mot",
}
SETUP_STEPS = ["预检（配置、容器、磁盘、授权软件包）", "拉取 BSP 的 4 个层（固定版本）", "安装可选组件",
               "写入基线 build/conf", "在容器里取出并打补丁：内核、U-Boot、TF-A 源码", "记录完成"]
BUILD_STEPS = ["预检", "安装缺少的可选组件", "生成 build/conf", "bitbake", "收集产物"]


def release() -> dict:
    return tomllib.loads(CATALOG.read_text())["releases"][RELEASE]


def component(name: str, rel: dict | None = None) -> dict:
    opts = (rel or release())["optional"]
    for c in opts:
        if c["name"] == name:
            return c
    raise JobError(f"没有可选组件 {name!r}；可选的有：{', '.join(c['name'] for c in opts)}")


def build_files(image: str) -> dict[str, str]:
    return {k: v.format(image=image) for k, v in BUILD_FILES.items()}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _project(cfg: config.Config) -> Path:
    miss = cfg.missing("yocto.project", "yocto.container")
    if miss:
        raise JobError(f"config.toml 里没有设置 {', '.join(miss)}：{cfg.path}", "在 config.toml 里补上后重试。")
    return cfg.yocto_project


def _git_rev(path: Path) -> str | None:
    """HEAD of the git repository holding `path` (None: not in a git repository)."""
    if not path.is_dir():
        return None
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _present(project: Path, c: dict) -> bool:
    return all((project / l / "conf" / "layer.conf").exists() for l in c["layers"])


def _free_gb(path: Path) -> float:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / 2**30


# ---------------------------------------------------------------- container, locks

def container_info(cfg: config.Config, path: Path | None = None) -> dict:
    """exists None: docker not usable; mounts: the container bind-mounts `path` (default: the project) as itself."""
    path = Path(path or cfg.yocto_project or "/")
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{json .State.Running}}\t{{json .Mounts}}", cfg.container or ""],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"name": cfg.container, "exists": None, "running": False, "mounts": False, "error": str(e)}
    if r.returncode != 0:
        docker_ok = "No such object" in r.stderr or "no such" in r.stderr.lower()
        return {"name": cfg.container, "exists": False if docker_ok else None, "running": False, "mounts": False,
                "error": r.stderr.strip()[-300:]}
    running, mounts = r.stdout.strip().split("\t", 1)
    ok = any(m.get("Source") == m.get("Destination") and path.is_relative_to(m["Source"])
             for m in json.loads(mounts or "[]") if m.get("Source"))
    return {"name": cfg.container, "exists": True, "running": running == "true", "mounts": ok}


def _ensure_container(cfg: config.Config, paths: list[Path]) -> None:
    c = container_info(cfg)
    if not c["exists"]:
        raise JobError(f"构建容器 {cfg.container} 不可用：{c.get('error', '')}",
                       "确认 docker 能用、容器存在（config.toml yocto.container），然后重试。")
    for p in paths:
        if not container_info(cfg, p)["mounts"]:
            raise JobError(f"构建容器 {cfg.container} 没有把 {p} 挂载到同一路径，bitbake 在容器里看不到它。",
                           "把它放到容器挂载的目录下（docker inspect 看 Mounts），或者重建容器加上这个挂载。")
    if not c["running"]:
        r = subprocess.run(["docker", "start", cfg.container], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise JobError(f"docker start {cfg.container} 失败：{r.stderr.strip()[-300:]}", "检查 docker 后重试。")


def bitbake_busy(build: Path) -> bool:
    """True while a bitbake server holds <build>/bitbake.lock (bb.utils.lockfile: flock)."""
    lock = build / "bitbake.lock"
    if not lock.exists():
        return False
    try:
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def running(cfg: config.Config | None = None) -> dict:
    """The rz-mcp job holding the "yocto" lock (or None) and whether a bitbake runs in <project>/build."""
    cfg = cfg or config.load()
    fd = jobs._try_lock(LOCK, config.JOBS_DIR)
    job = None
    if fd is None:
        owner = jobs.lock_owner(LOCK, config.JOBS_DIR)
        m = re.search(r"作业 ([\w.-]+)", owner)
        job = {"id": m.group(1) if m else None, "owner": owner}
    else:
        os.close(fd)
    p = cfg.yocto_project
    return {"job": job, "bitbake": bool(p) and bitbake_busy(p / "build")}


# ---------------------------------------------------------------- is the project usable?

def _template_layers(project: Path, rel: dict) -> list[Path]:
    text = (project / rel["template"] / "bblayers.conf.sample").read_text()
    return [project / m for m in re.findall(r"\$\{TOPDIR\}/\.\./(\S+)", text)]


def _check(project: Path, rel: dict) -> list[str]:
    """What is missing or different from the release (empty: the project is the release)."""
    probs = []
    if not (project / "poky" / "oe-init-build-env").exists():
        probs.append("没有 poky/oe-init-build-env")
    if not (project / rel["template"]).is_dir():
        probs.append(f"没有发布的配置模板 {rel['template']}")
        return probs
    for l in _template_layers(project, rel):
        if not (l / "conf" / "layer.conf").exists():
            probs.append(f"缺少官方模板里的层 {l.relative_to(project)}")
    pv = project / "poky" / "meta-poky" / "conf" / "distro" / "poky.conf"
    m = re.search(r'^DISTRO_VERSION\s*=\s*"([^"]+)"', pv.read_text(), re.M) if pv.exists() else None
    if not m or m.group(1) != rel["poky_version"]:
        probs.append(f"poky 的 DISTRO_VERSION 是 {m.group(1) if m else '?'}，{RELEASE} 是 {rel['poky_version']}")
    for n, c in rel["components"].items():
        recipe = project / "meta-renesas" / c["recipe"]
        text = recipe.read_text() if recipe.exists() else ""
        pins = re.findall(rf'^{c["rev_var"]}(?::rzg3l-family)?\s*\??=\s*"([0-9a-f]+)"', text, re.M)
        if c["rev"] not in pins:
            probs.append(f"meta-renesas 的 {n} 版本（{c['recipe']}）不是 {RELEASE} 的 {c['rev'][:12]}")
    return probs


def sources(project: Path, rel: dict | None = None) -> dict[str, list[str]]:
    """The fetched and patched official source trees in the build directory (component -> existing paths)."""
    rel = rel or release()
    return {n: sorted(glob.glob(str(project / "build" / c["work"]))) for n, c in rel["components"].items()}


def conf_info(project: Path) -> dict:
    """Which configuration build/conf holds, as recorded by rz-mcp in build/conf/rz-mcp.json."""
    conf = project / "build" / "conf"
    if not (conf / "local.conf").exists() or not (conf / "bblayers.conf").exists():
        return {"exists": False}
    meta = _read_json(conf / "rz-mcp.json")
    if not meta:
        return {"exists": True, "config": None}
    sha = _conf_sha((conf / "bblayers.conf").read_text(), (conf / "local.conf").read_text())
    return {"exists": True, **meta, "modified": sha != meta.get("conf_sha256")}


def assess(cfg: config.Config | None = None) -> dict:
    """Classify config.yocto.project as a Yocto project of RZG3L-BSP-1.0.0:

    absent      does not exist or is empty: yocto_setup creates it
    incomplete  a yocto_setup that did not finish (or a project it made that lost a part): yocto_setup continues
    usable      the release's layers are there and are the release (warnings are informational)
    unusable    anything else (problems say why): no tool changes it
    """
    cfg = cfg or config.load()
    rel = release()
    p = cfg.yocto_project
    out = {"project": str(p) if p else None, "release": RELEASE, "state": "unusable", "problems": [], "warnings": [],
           "components": {}, "setup": None}
    if p is None:
        out["problems"].append(f"config.toml 里没有设置 yocto.project（{cfg.path}）")
        return out
    if not p.exists():
        out["state"] = "absent"
        return out
    if not p.is_dir():
        out["problems"].append("不是目录")
        return out
    st = out["setup"] = _read_json(p / STATE_DIR / "project.json")
    entries = sorted(e.name for e in p.iterdir() if e.name not in IGNORABLE)
    ours = (st or {}).get("created_by") == "rz-mcp"
    if not entries and not ours:
        out["state"] = "absent"
        return out
    probs = _check(p, rel)
    for c in rel["optional"]:
        if _present(p, c):
            r = ((st or {}).get("components") or {}).get(c["name"])
            out["components"][c["name"]] = {"version": r["version"] if r else None, "layers": c["layers"],
                                            "installed": r["installed"] if r else None}
    if ours and (probs or not st.get("done")):
        out["state"] = "incomplete"
        out["problems"] = probs
        return out
    if probs:
        out["problems"] = [f"目录不是空的，也不是 {RELEASE} 的 Yocto 项目（里面有：{', '.join(entries[:8])}"
                           f"{' ...' if len(entries) > 8 else ''}）"] + probs
        return out
    out["state"] = "usable"
    for l in rel["layers"]:
        head = _git_rev(p / l["name"]) if (p / l["name"] / ".git").exists() else None
        if head is None:
            out["warnings"].append(f"{l['name']} 不是 git 仓库，无法核对版本")
        elif head != l["rev"]:
            out["warnings"].append(f"{l['name']} 当前在 {head[:12]}，发布固定的是 {l['rev'][:12]}")
    missing = [n for n, v in sources(p, rel).items() if not v]
    if missing:
        out["warnings"].append(f"还没有取出 {', '.join(missing)} 的源码：yocto_setup 会补上")
    if not conf_info(p)["exists"]:
        out["warnings"].append("还没有 build/conf：yocto_setup 会写入基线配置")
    return out


# ---------------------------------------------------------------- running commands with streamed output

class _Throttle:
    def __init__(self, ctx, every: float = 1.0):
        self.ctx, self.every, self.t = ctx, every, 0.0

    def __call__(self, text: str) -> None:
        if time.time() - self.t >= self.every:
            self.t = time.time()
            self.ctx.progress(text)


def _stream(cmd: list[str], on_line, abort=None) -> int:
    """Run cmd, calling on_line for every output line ('\\r' ends a line too); abort(p) if interrupted."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    buf = b""
    try:
        while chunk := p.stdout.read1(65536):
            *lines, buf = re.split(rb"[\r\n]", buf + chunk)
            for l in lines:
                if l.strip():
                    on_line(l.decode(errors="replace").rstrip())
        if buf.strip():
            on_line(buf.decode(errors="replace").rstrip())
        return p.wait()
    finally:
        if p.poll() is None:
            (abort or (lambda q: None))(p)
            if p.poll() is None:
                p.kill()
            p.wait()


def _git(ctx, args: list[str], what: str) -> None:
    prog = _Throttle(ctx)
    rc = _stream(["git", *args], lambda l: prog(f"{what}: {l}") if "%" in l else ctx.log(l))
    if rc != 0:
        raise JobError(f"{what}：git {' '.join(args[:2])} 失败（退出码 {rc}），详见作业日志。",
                       "检查网络后重新调用 yocto_setup，它会从当前状态接着做。")


def _clone_at(ctx, project: Path, url: str, rev: str, dest: Path, tag: str | None = None) -> None:
    """git clone + checkout -b tmp <rev> (How To Build), staged in .rz-mcp/stage and renamed into place."""
    label = str(dest.relative_to(project))
    if dest.exists():
        head = _git_rev(dest) if (dest / ".git").exists() else None
        if head == rev:
            ctx.log(f"{label}: 已在 {rev[:12]}，跳过", key=True)
            return
        raise JobError(f"{dest} 已经存在，但不是 {rev[:12]} 的 git 检出（HEAD {head[:12] if head else '无'}），没有改动它。",
                       "这不是 yocto_setup 创建的内容：换一个 yocto.project 目录，或者手动处理它。")
    stage = project / STATE_DIR / "stage" / dest.name
    shutil.rmtree(stage, ignore_errors=True)
    stage.parent.mkdir(parents=True, exist_ok=True)
    ctx.log(f"{label}: git clone {url}", key=True)
    _git(ctx, ["clone", "--progress", "--no-checkout", url, str(stage)], label)
    if tag:
        r = subprocess.run(["git", "-C", str(stage), "rev-parse", f"{tag}^{{commit}}"], capture_output=True, text=True)
        if r.stdout.strip() != rev:
            raise JobError(f"{url} 的 tag {tag} 现在指向 {r.stdout.strip()[:12] or '?'}，发布固定的是 {rev[:12]}，没有使用它。",
                           "上游改动了 tag：请维护者核对 catalog/bsp_releases.toml。")
    _git(ctx, ["-C", str(stage), "checkout", "--quiet", "-b", "tmp", rev], label)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage.rename(dest)
    ctx.log(f"{label}: {rev[:12]}" + (f"（tag {tag}）" if tag else ""), key=True)


# ---------------------------------------------------------------- optional components

def package_file(cfg: config.Config, c: dict, check_sha: bool = True) -> Path:
    """The licensed package of a component; JobError naming the file if it is missing or not the expected one."""
    f = cfg.packages / c["file"]
    if not f.exists():
        raise JobError(f"可选组件 {c['name']} 需要授权软件包 {f.name}（{c['part']} {c['version']}），但 {f} 不存在。"
                       "还没有做任何改动。",
                       f"从 renesas.com 下载 {c['part']} {c['version']}（{c.get('source', '')}），"
                       f"把 {f.name} 放到 {f.parent}/，然后重新调用。")
    if check_sha:
        got = _sha256(f)
        if got != c["sha256"]:
            raise JobError(f"{f} 的 sha256 是 {got}，{RELEASE} 需要的是 {c['sha256']}（版本不对或文件损坏），没有安装。",
                           f"重新下载 {c['part']} {c['version']} 替换它，然后重新调用。")
    return f


def _member(zf: zipfile.ZipFile, spec: str):
    if "!" in spec:                       # a tar.gz inside a zip inside the package
        outer, inner = spec.split("!", 1)
        return zipfile.ZipFile(io.BytesIO(zf.read(outer))).open(inner)
    return zf.open(spec)


def _unpack(stream, dest: Path, spec: str) -> int:
    def only_features(member, path):
        member = tarfile.data_filter(member, path)   # no absolute paths, '..', links out, devices
        name = member.name.removeprefix("./")
        if name != "meta-rz-features" and not name.startswith("meta-rz-features/"):
            raise JobError(f"{spec} 里有 meta-rz-features/ 以外的内容 {member.name!r}，没有安装。")
        return member
    n = 0
    with tarfile.open(fileobj=stream, mode="r|gz") as tf:
        for m in tf:
            tf.extract(m, dest, filter=only_features)
            n += 1
    return n


def install_component(ctx, cfg: config.Config, project: Path, name: str) -> str:
    """Install one optional component if its layers are not there yet (shared by yocto_setup and poc_build)."""
    c = component(name)
    if _present(project, c):
        return f"{name}: 已安装"
    if c["kind"] == "git":
        _clone_at(ctx, project, c["url"], c["rev"], project / c["dest"], c.get("tag"))
        rec = {"kind": "git", "version": c["version"], "url": c["url"], "rev": c["rev"]}
    else:
        f = package_file(cfg, c)
        stage = project / STATE_DIR / "stage" / name
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        try:
            with zipfile.ZipFile(f) as zf:
                for spec in c["extract"]:
                    with _member(zf, spec) as s:
                        ctx.log(f"{name}: 解压 {spec.split('/')[-1]}（{_unpack(s, stage, spec)} 项）", key=True)
        except (zipfile.BadZipFile, KeyError, tarfile.TarError, OSError) as e:
            raise JobError(f"{name}: 解压 {f.name} 失败：{type(e).__name__}: {e}")
        for l in c["layers"]:
            if not (stage / l / "conf" / "layer.conf").exists():
                raise JobError(f"{name}: {f.name} 里没有 {l}/conf/layer.conf")
        dest = project / "meta-rz-features"
        dest.mkdir(exist_ok=True)
        items = sorted((stage / "meta-rz-features").iterdir())
        clash = [str(dest / i.name) for i in items if (dest / i.name).exists()]
        if clash:
            raise JobError(f"{name}: {', '.join(clash)} 已经存在，没有覆盖。", "确认它是什么后手动移走，然后重试。")
        for i in items:
            i.rename(dest / i.name)
        shutil.rmtree(stage, ignore_errors=True)
        rec = {"kind": "package", "version": c["version"], "part": c["part"], "file": f.name, "sha256": c["sha256"]}
    st = _read_json(project / STATE_DIR / "project.json") or {}
    st.setdefault("components", {})[name] = {**rec, "installed": _now()}
    _write_json(project / STATE_DIR / "project.json", st)
    msg = f"{name}: 已安装 {c['version']} -> {', '.join(c['layers'])}"
    ctx.log(msg, key=True)
    return msg


# ---------------------------------------------------------------- build/conf

def _conf_sha(bblayers: str, local: str) -> str:
    return hashlib.sha256(bblayers.encode() + b"\0" + local.encode()).hexdigest()


def write_conf(project: Path, poc: str | None = None, layers: list[Path] = (), local_conf: str = "",
               by: str = "poc_build") -> dict:
    """build/conf = release template + rz-mcp standard + extra layers + extra local.conf lines; the previous
    files go to build/conf/backup/<time>/. Returns what build/conf/rz-mcp.json records."""
    rel = release()
    t = project / rel["template"]
    label = f"PoC {poc}" if poc else "baseline"
    head = f"# Written by rz-mcp {by} ({label}) from {rel['template']}; every poc_build regenerates it.\n"
    bbl = head + (t / "bblayers.conf.sample").read_text().replace("##OEROOT##", str(project / "poky"))
    if layers:
        bbl += ('\n# ---- rz-mcp: optional components and PoC layers ----\nBBLAYERS += " \\\n'
                + "".join(f"  {l} \\\n" for l in layers) + '  "\n')
    local = head + (t / "local.conf.sample").read_text().rstrip("\n")
    local += "\n\n# ---- rz-mcp standard ----\n" + "\n".join(STANDARD) + "\n"
    if local_conf.strip():
        local += f"\n# ---- {poc}/build.toml local_conf ----\n" + local_conf.strip("\n") + "\n"
    conf = project / "build" / "conf"
    sha = _conf_sha(bbl, local)
    names = ("bblayers.conf", "local.conf", "templateconf.cfg", "rz-mcp.json")
    if any((conf / n).exists() for n in names[:2]) and conf_info(project).get("conf_sha256") != sha:
        backup = conf / "backup" / time.strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True, exist_ok=True)
        for n in names:
            if (conf / n).exists():
                shutil.copy2(conf / n, backup / n)
        for old in sorted((conf / "backup").iterdir())[:-10]:     # keep the last 10
            shutil.rmtree(old, ignore_errors=True)
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "bblayers.conf").write_text(bbl)
    (conf / "local.conf").write_text(local)
    (conf / "templateconf.cfg").write_text(f"{t}\n")
    meta = {"config": label, "poc": poc, "by": by, "written": _now(), "template": rel["template"],
            "layers": [str(l) for l in layers], "conf_sha256": sha}
    _write_json(conf / "rz-mcp.json", meta)
    return meta


# ---------------------------------------------------------------- bitbake in the container

def run_bitbake(ctx, cfg: config.Config, args: list[str]) -> dict:
    """bitbake <args> in the container with nice; output into the job log. Returns rc, errors, failed tasks."""
    project = cfg.yocto_project
    build = project / "build"
    for _ in range(BITBAKE_WAIT):           # a cancelled bitbake may still be shutting down
        if not bitbake_busy(build):
            break
        ctx.progress("等待上一个 bitbake 退出（build/bitbake.lock）")
        ctx.check_cancel()
        time.sleep(1)
    else:
        raise JobError(f"bitbake 正在 {build} 里运行（不是 rz-mcp 的作业启动的，或者没能退出）。",
                       "等它结束（或在容器里停掉它）后再调用。")
    _ensure_container(cfg, [project])
    pidfile = project / STATE_DIR / "bitbake.pid"
    pidfile.unlink(missing_ok=True)
    cmdline = "bitbake " + " ".join(shlex.quote(a) for a in args)
    script = (f"cd {shlex.quote(str(project))} && source poky/oe-init-build-env build >/dev/null && "
              f"echo $$ > {shlex.quote(str(pidfile))} && exec nice -n 10 {cmdline}")
    ctx.log(f"{cmdline}（容器 {cfg.container}，日志 {ctx.dir / 'log.txt'}）", key=True)
    info = {"errors": [], "failed": [], "logs": [], "summary": None}
    prog = _Throttle(ctx)

    def on_line(l: str) -> None:
        key = l.startswith(("ERROR:", "NOTE: Tasks Summary", "Summary:"))
        ctx.log(l, key=key)
        if m := re.match(r"NOTE: Running (setscene )?task (\d+) of (\d+) \((.*)\)", l):
            prog(f"{'setscene ' if m.group(1) else ''}task {m.group(2)}/{m.group(3)} {Path(m.group(4)).name}")
        elif l.startswith("ERROR:"):
            info["errors"].append(l)
            if m := re.match(r"ERROR: Task \((.+)\) failed", l):
                info["failed"].append(m.group(1))
            if m := re.search(r"Logfile of failure stored in: (\S+)", l):
                info["logs"].append(m.group(1))
        elif "Tasks Summary" in l:
            info["summary"] = l

    def abort(p) -> None:                   # killing `docker exec` does not stop bitbake: signal it in the container
        try:
            pid = int(pidfile.read_text())
        except (OSError, ValueError):
            return
        for wait in (1.5, 3.0):
            subprocess.run(["docker", "exec", cfg.container, "kill", "-INT", str(pid)], capture_output=True, timeout=3)
            try:
                p.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                pass

    try:
        info["rc"] = _stream(["docker", "exec", "-e", "PYTHONUNBUFFERED=1", cfg.container, "bash", "-c", script],
                             on_line, abort)
    finally:
        pidfile.unlink(missing_ok=True)
    return info


def _bitbake_error(info: dict, ctx) -> JobError:
    lines = [f"bitbake 失败（退出码 {info['rc']}）。"]
    if info["failed"]:
        lines.append("失败的任务：" + "；".join(info["failed"][:5]))
    if info["errors"]:
        lines.append("最先的错误：\n  " + "\n  ".join(e[:400] for e in info["errors"][:5]))
    lines.append("日志：" + "；".join(info["logs"][:3] + [str(ctx.dir / "log.txt")]))
    return JobError("\n".join(lines), "看出错任务的日志，修改层（或 build.toml）后再次调用 poc_build。")


# ---------------------------------------------------------------- yocto_setup job

def _setup_job(ctx) -> dict:
    cfg, rel = config.load(), release()
    names = list(dict.fromkeys(ctx.params.get("components") or []))
    ctx.step(1)
    p = _project(cfg)
    comps = [component(n, rel) for n in names]
    a = assess(cfg)
    if a["state"] == "unusable":
        raise JobError(f"{p} 不可用，没有做任何改动：" + "；".join(a["problems"]),
                       "在 config.toml 里把 yocto.project 改成一个不存在或空的目录，再调用 yocto_setup。")
    _ensure_container(cfg, [p])
    if not shutil.which("git"):
        raise JobError("主机上没有 git", "安装 git 后重试。")
    free = _free_gb(p)
    if free < MIN_FREE_GB:
        raise JobError(f"{p} 所在磁盘只剩 {free:.0f} GB，至少需要 {MIN_FREE_GB} GB（构建一个镜像约 60 GB）。",
                       "清理磁盘后重试。")
    if free < WARN_FREE_GB:
        ctx.log(f"提醒：只剩 {free:.0f} GB，构建一个镜像约 60 GB", key=True)
    for c in comps:
        if c["kind"] == "package" and not _present(p, c):
            package_file(cfg, c)              # a missing or wrong package fails before anything changes
    ctx.log(f"{p}: {a['state']}", key=True)
    p.mkdir(parents=True, exist_ok=True)
    sf = p / STATE_DIR / "project.json"
    st = _read_json(sf) or {}
    if a["state"] == "absent" and not st:
        st = {"created_by": "rz-mcp", "release": RELEASE, "created": _now(), "done": None, "components": {}}
    _write_json(sf, {**st, "release": RELEASE})

    ctx.step(2)
    if a["state"] == "usable":
        ctx.log("层已经齐全且是这个发布的版本：直接复用", key=True)
    else:
        for l in rel["layers"]:
            ctx.check_cancel()
            _clone_at(ctx, p, l["url"], l["rev"], p / l["name"], l.get("tag"))

    ctx.step(3)
    done = [install_component(ctx, cfg, p, n) for n in names] or ["没有要求安装可选组件"]

    ctx.step(4)
    if conf_info(p)["exists"]:
        ctx.log("build/conf 已经存在：保留它", key=True)
    else:
        write_conf(p, by="yocto_setup")
        ctx.log("写入了基线 build/conf（官方模板 + rz-mcp 标准项）", key=True)

    ctx.step(5)
    t0 = time.time()
    if all(sources(p, rel).values()):
        ctx.log("内核、U-Boot、TF-A 的源码已经取出：跳过", key=True)
    else:
        recipes = [c["recipe_name"] for c in rel["components"].values()]
        info = run_bitbake(ctx, cfg, ["-c", "patch", *recipes])
        if info["rc"] != 0:
            e = _bitbake_error(info, ctx)
            raise JobError(str(e), "多半是网络问题：检查后重新调用 yocto_setup，它会接着做。")
        missing = [n for n, v in sources(p, rel).items() if not v]
        if missing:
            raise JobError(f"bitbake -c patch 成功了，但没有找到 {', '.join(missing)} 的源码目录。", "这是 rz-mcp 的缺陷。")

    ctx.step(6)
    probs = _check(p, rel)
    if probs:
        raise JobError("搭建后项目仍不完整：" + "；".join(probs), "重新调用 yocto_setup。")
    st = _read_json(sf) or {}
    st["done"] = st.get("done") or _now()
    st["last_setup"] = _now()
    _write_json(sf, st)
    srcs = sources(p, rel)
    ctx.report(f"# yocto_setup\n\n项目：{p}（{RELEASE}）\n\n" + "\n".join(f"- {d}" for d in done)
               + "\n\n源码：\n" + "\n".join(f"- {n}: {', '.join(v)}" for n, v in srcs.items())
               + f"\n\n取源码用时 {time.time() - t0:.0f} 秒。\n")
    return {"summary": f"{p} 可用（{RELEASE}，{a['state']} -> usable）。" + "；".join(done)
                       + "。源码：" + "；".join(f"{n} {v[0]}" for n, v in srcs.items() if v),
            "next": "yocto_status 查看源码和产物的位置；poc_build 构建镜像。", "project": str(p), "sources": srcs}


# ---------------------------------------------------------------- poc_build job

def read_build_toml(name: str, cfg: config.Config) -> dict:
    """<poc>/build.toml checked: image, components, layers (resolved, must be layers), local_conf."""
    try:
        d = pocmod.repo(name, cfg)
    except pocmod.PocError as e:
        raise JobError(str(e), "用 poc_status() 列出现有的 PoC。")
    f = d / "build.toml"
    if not f.exists():
        raise JobError(f"{f} 不存在：poc_build 从这里读取 PoC 的镜像、可选组件、层和 local.conf。",
                       "按 poc_create 生成的格式写好 build.toml 后再调用 poc_build。")
    try:
        spec = tomllib.loads(f.read_text())
    except tomllib.TOMLDecodeError as e:
        raise JobError(f"{f} 不是合法的 TOML：{e}", "改正后再调用 poc_build。")
    out = {"image": spec.get("image") or None, "components": list(spec.get("components") or []), "layers": [],
           "local_conf": spec.get("local_conf") or ""}
    for n in out["components"]:
        component(n)
    for l in spec.get("layers") or []:
        p = Path(l).expanduser()
        p = (p if p.is_absolute() else d / p).resolve()
        if not (p / "conf" / "layer.conf").exists():
            raise JobError(f"{f}: layers 里的 {l} 不是层（没有 {p}/conf/layer.conf）。", "改正 build.toml 后再调用 poc_build。")
        out["layers"].append(p)
    if out["image"] and not NAME_RE.match(out["image"]):
        raise JobError(f"{f}: image {out['image']!r} 不是镜像名", "改正 build.toml 后再调用 poc_build。")
    return out


def collect(project: Path, image: str, dest: Path | None = None) -> tuple[Path, dict, dict]:
    """The build's files from the deploy directory (symlinks resolved); with dest, copied there (replacing the
    previous copy, names without timestamp). Returns (dir, {key: path}, {key: sha256})."""
    deploy = project / "build" / DEPLOY
    found = {k: (deploy / n).resolve() for k, n in build_files(image).items() if (deploy / n).exists()}
    if "rootfs" not in found:
        raise JobError(f"bitbake 成功了，但 {deploy} 里没有 {build_files(image)['rootfs']}。",
                       "确认这个镜像会生成 wic.gz（IMAGE_FSTYPES），改正后再调用 poc_build。")
    if dest is not None:
        tmp = dest.with_name(dest.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for k, src in found.items():
            shutil.copy2(src, tmp / build_files(image)[k])
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        found = {k: dest / build_files(image)[k] for k in found}
    return (dest or deploy), {k: str(v) for k, v in found.items()}, {k: _sha256(Path(v)) for k, v in found.items()}


def last_build(cfg: config.Config | None = None) -> dict | None:
    """Record of the last poc_build in the shared project (ok or not), or None."""
    cfg = cfg or config.load()
    return _read_json(cfg.yocto_project / STATE_DIR / "last-build.json") if cfg.yocto_project else None


def _save_record(cfg: config.Config, rec: dict) -> None:
    _write_json(cfg.yocto_project / STATE_DIR / "last-build.json", {"time": _now(), **rec})
    if rec.get("poc"):
        try:
            pocmod.record(rec["poc"], "build", rec, cfg)
        except pocmod.PocError:
            pass


def _build_inputs(cfg: config.Config, name: str | None, image: str | None, check_sha: bool):
    """Checks shared by poc_build's front end and its job: (project, build.toml spec, image, components)."""
    p = _project(cfg)
    if image and not NAME_RE.match(image):
        raise JobError(f"{image!r} 不是镜像名", "例如 core-image-minimal。")
    spec = read_build_toml(name, cfg) if name else {"image": None, "components": [], "layers": [], "local_conf": ""}
    a = assess(cfg)
    if a["state"] != "usable":
        raise JobError(f"Yocto 项目 {p} 现在是 {a['state']}" + ("：" + "；".join(a["problems"]) if a["problems"] else ""),
                       "先调用 yocto_setup 搭好（或修好）它，再调用 poc_build。")
    comps = [component(n) for n in spec["components"]]
    for c in comps:
        if c["kind"] == "package" and not _present(p, c):
            package_file(cfg, c, check_sha)            # a missing package fails before anything changes
    return p, spec, image or spec["image"] or cfg.default_image, comps


def _build_job(ctx) -> dict:
    cfg, t0 = config.load(), time.time()
    name = ctx.params.get("poc")
    rec = {"ok": False, "job": ctx.job["id"], "poc": name, "image": ctx.params.get("image") or cfg.default_image}
    try:
        ctx.step(1)
        p, spec, image, comps = _build_inputs(cfg, name, ctx.params.get("image"), check_sha=True)
        rec["image"] = image
        inputs = pocmod.inputs_sha256(name, cfg) if name else None
        _ensure_container(cfg, [p, *spec["layers"]])
        ctx.step(2)
        for c in comps:
            install_component(ctx, cfg, p, c["name"])
        ctx.step(3)
        layers = [p / l for c in comps for l in c["layers"]] + spec["layers"]
        meta = write_conf(p, name, layers, spec["local_conf"])
        ctx.log(f"build/conf = {meta['config']}（{len(layers)} 个额外的层，sha256 {meta['conf_sha256'][:12]}）", key=True)
        ctx.step(4)
        test_args = ctx.params.get("bitbake_args")          # tests only: a cheap target instead of the image
        info = run_bitbake(ctx, cfg, test_args or [image])
        if info["rc"] != 0:
            raise _bitbake_error(info, ctx)
        if test_args:
            return {"summary": f"bitbake {' '.join(test_args)} 成功", "next": "无（测试）", "bitbake": info}
        ctx.step(5)
        dest = pocmod.repo(name, cfg) / pocmod.STATE_DIR / "build" if name else None
        d, files, sha = collect(p, image, dest)
        all_layers = _template_layers(p, release()) + layers
        rec.update(ok=True, dir=str(d), files=files, sha256=sha, conf_sha256=meta["conf_sha256"],
                   layers=[{"path": str(l), "git": _git_rev(l)} for l in all_layers], seconds=round(time.time() - t0))
        if name:
            rec["inputs_sha256"] = inputs
        _save_record(cfg, rec)
    except (JobError, jobs.Cancelled) as e:
        msg = str(e) if isinstance(e, JobError) else "作业被取消"
        rec.update(error=msg.splitlines()[0][:300] if msg else "失败", log=str(ctx.dir / "log.txt"))
        if cfg.yocto_project and cfg.yocto_project.is_dir():
            _save_record(cfg, rec)
        raise
    flash = f'board_flash(poc="{name}", parts=...)' if name else f'board_flash(image="{files["rootfs"]}", parts=...)'
    ctx.report(f"# poc_build {image}\n\n配置：{meta['config']}\n\n用时 {rec['seconds']} 秒\n\n"
               + "\n".join(f"- {k}: {v}" for k, v in files.items()) + "\n")
    return {"summary": f"构建成功（{meta['config']}，{image}，{rec['seconds']} 秒）。rootfs：{files['rootfs']}；"
                       f"共 {len(files)} 个文件在 {d}",
            "next": f"{flash} 把它写进 EVK。", "record": rec}


# ---------------------------------------------------------------- tool front ends (devtools)

def _busy_text(owner: str, tool: str) -> str:
    return (f"没有启动：共用的 Yocto 项目正被{owner}使用（同一时间只运行一个 bitbake）。"
            f"用 job_status 查看它，等它结束（或用 job_cancel 取消它）后再调用 {tool}。")


def tool_status() -> str:
    cfg = config.load()
    miss = cfg.missing("yocto.project", "yocto.container")
    if miss:
        return f"config.toml（{cfg.path}）里没有设置 {', '.join(miss)}。补上后再调用 yocto_status。"
    a, p, rel = assess(cfg), cfg.yocto_project, release()
    head = {"absent": "不存在或是空目录，可以搭建", "incomplete": "上次的搭建没有完成，可以接着做",
            "usable": "可用", "unusable": "不可用（不是这个发布的 Yocto 项目，任何工具都不会改动它）"}[a["state"]]
    L = [f"Yocto 项目 {p}（{RELEASE}，MACHINE {MACHINE}）：{head}"]
    L += [f"  - 问题：{x}" for x in a["problems"]] + [f"  - 提醒：{x}" for x in a["warnings"]]
    c = container_info(cfg)
    L.append(f"构建容器 {cfg.container}：" + ("不可用（" + c.get("error", "") + "）" if not c["exists"] else
             ("运行中" if c["running"] else "已停止（作业会自动启动它）") + ("，挂载了项目目录" if c["mounts"] else "，没有挂载项目目录！")))
    have = a["components"]
    L.append("可选组件：" + "；".join(
        f"{o['name']} " + (f"已安装 {have[o['name']]['version'] or '（不是 rz-mcp 装的，版本未知）'}"
                           if o["name"] in have else "未安装") for o in rel["optional"]))
    run = running(cfg)
    if a["state"] in ("usable", "incomplete"):
        ci = conf_info(p)
        L.append("build/conf：" + ("还没有" if not ci["exists"] else "不是 rz-mcp 写的" if not ci.get("config") else
                 f"{ci['config']}（{ci['by']} 写于 {ci['written']}" + ("，之后被手动改过" if ci["modified"] else "") + "）"))
    L.append("正在运行：" + ("；".join(filter(None, [run["job"] and run["job"]["owner"],
                                                 run["bitbake"] and f"bitbake（{p}/build/bitbake.lock）"])) or "没有"))
    if a["state"] == "usable":
        L.append("层：" + "；".join(str(p / l["name"]) for l in rel["layers"])
                 + "".join(f"；{p / l}" for n in have for l in have[n]["layers"]))
        srcs = sources(p, rel)
        L.append("官方源码（上一次 bitbake 打完补丁后的样子；构建过 PoC 时含 PoC 的补丁）：")
        L += [f"  - {n}: {', '.join(v) if v else '还没有取出（调用 yocto_setup）'}" for n, v in srcs.items()]
        L.append(f"产物目录：{p / 'build' / DEPLOY}")
        lb = last_build(cfg)
        if lb:
            L.append(f"上次构建：{'成功' if lb['ok'] else '失败'}，{lb.get('poc') or 'baseline'}，{lb['image']}，{lb.get('time', '')}，"
                     + (f"rootfs {lb['files'].get('rootfs')}" if lb["ok"] else f"{lb.get('error', '')}（日志 {lb.get('log')}）"))
    if run["job"]:
        nxt = f"等作业 {run['job']['id']} 结束：job_status(job_id=\"{run['job']['id']}\", wait_seconds=50)。"
    elif a["state"] in ("absent", "incomplete"):
        nxt = "yocto_setup（需要可选组件时传 components）。"
    elif a["state"] == "unusable":
        nxt = "告诉用户这个目录不能用：在 config.toml 里把 yocto.project 改成不存在或空的目录，再调用 yocto_setup。"
    elif a["warnings"] and any("yocto_setup" in w for w in a["warnings"]):
        nxt = "yocto_setup 补上缺的部分；之后 poc_build 构建镜像。"
    else:
        nxt = "poc_build 构建镜像（需要未安装的可选组件时先 yocto_setup(components=[...])）；BSP 源码用你自己的文件工具直接读上面的路径。"
    L.append("下一步：" + nxt)
    return "\n".join(L)


def tool_setup(components: list[str] | None = None) -> str:
    cfg = config.load()
    names = list(dict.fromkeys(components or []))
    if cfg.yocto_project and (job := running(cfg)["job"]):
        return _busy_text(job["owner"], "yocto_setup")
    try:
        p = _project(cfg)
        comps = [component(n) for n in names]
        a = assess(cfg)
        if a["state"] == "unusable":
            return (f"没有启动：{p} 不可用，没有改动它。\n" + "\n".join(f"  - {x}" for x in a["problems"])
                    + "\n下一步：告诉用户在 config.toml 里把 yocto.project 改成一个不存在或空的目录，再调用 yocto_setup。")
        todo = [c for c in comps if not _present(p, c)] if p.is_dir() else comps
        for c in todo:
            if c["kind"] == "package":
                package_file(cfg, c, check_sha=False)
        if a["state"] == "usable" and not todo and not a["warnings"]:
            return (f"{p} 已经可用（{RELEASE}），" + (f"{', '.join(names)} 已安装，" if names else "")
                    + "不需要再搭建。\n" + tool_status())
        st = jobs.start("yocto_setup", {"components": names}, title=f"搭建 Yocto 项目 {p}" +
                        (f"（{', '.join(names)}）" if names else ""), steps=SETUP_STEPS, lock=LOCK)
    except JobError as e:
        return f"没有启动：{e}\n建议：{e.hint}"
    except jobs.Busy as e:
        return _busy_text(e.owner, "yocto_setup")
    return jobs.format_started(st)


def tool_build(poc: str | None = None, image: str | None = None) -> str:
    cfg = config.load()
    if job := running(cfg)["job"]:                     # Y4: one bitbake at a time
        return _busy_text(job["owner"], "poc_build")
    try:
        _, _, img, _ = _build_inputs(cfg, poc, image, check_sha=False)
        st = jobs.start("poc_build", {"poc": poc, "image": img}, title=f"构建 {img}（{f'PoC {poc}' if poc else 'baseline'}）",
                        steps=BUILD_STEPS, poc=poc, lock=LOCK)
    except JobError as e:
        return f"没有启动：{e}\n建议：{e.hint}"
    except jobs.Busy as e:
        return _busy_text(e.owner, "poc_build")
    return jobs.format_started(st) + "\n（完整构建 core-image-minimal 第一次约 1 小时以上，有缓存时几分钟。）"
