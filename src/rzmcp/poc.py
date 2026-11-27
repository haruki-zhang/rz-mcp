"""PoC repositories: <poc.root>/<name>/ (config.toml). Contract shared by the tools (P3 / P2 / P4):

    <name>/build.toml          what poc_build applies: image, optional components, layers, local.conf lines
    <name>/design/design.md    design + verification plan (prose); its first line records the user's confirmation
    <name>/design/verify.toml  the verification plan board_verify runs (board_verify(plan=<name>))
    <name>/.rz-poc/state.json  the one state file: every tool that is given a PoC name records its result here
    <name>/.rz-poc/build/      copy of the files of the PoC's last successful build (see yocto.BUILD_FILES)

state.json keys, each written by one tool through record() (record() adds "time" = "%Y-%m-%dT%H:%M:%S"
local time unless the data has one; the latest result replaces the previous one):
    created  poc_create   {"summary", "title", "guide" (docs/ file name), "layer",
                           "templates": {relpath: sha256 of the file as created}}   (design.md: without line 1)
    build    poc_build    {"ok", "job", "image", "dir", "files": {key: abs path}, "sha256": {key: hex},
                           "inputs_sha256", "layers": [{"path", "git"}], "conf_sha256", "seconds"}
                          or {"ok": False, "error", "log"};  keys: rootfs, bmap, bl2_emmc(_bin), bl2_qspi(_bin),
                          fip(_bin), flash_writer
    flash    board_flash  {"ok", "job", "parts" ("rootfs" | "bootloader" | "all"), "boot_device" ("emmc" | "qspi"),
                           "sha256": {key: hex}}  or {"ok": False, "job", "error"}
                          sha256 = what is on the board after this flash, keys as in build.files ("rootfs",
                          "bl2_emmc_bin", "fip_bin", ...): the files written, plus, when only the rootfs was
                          written, the bootloader entries carried over from the previous record if it was ok and had
                          the same boot_device (the bootloader on the board did not change). poc_status suggests
                          parts="rootfs" only when the build's fip / fip_bin sha256 is among these values, else "all".
    verify   board_verify {"ok" (true only if every check passed), "job", "report" (abs path of the report),
                           "passed" (int), "failed" (int)}  or {"ok": False, "job", "error"} if it could not run
    package  poc_package  {"ok", "job", "dir", "version", "image_sha256"}  or {"ok": False, "job", "error"}

What poc_status reads from them (nothing else, besides the template hashes, design.md line 1 and inputs_sha256()):
    build:  ok, inputs_sha256 (== inputs_sha256() now?), sha256.rootfs, sha256.fip / fip_bin, error, log, time
    flash:  ok, time, sha256.rootfs (== build.sha256.rootfs: the current build is on the board), any sha256 value
            equal to the build's fip / fip_bin (its bootloader is on the board too), boot_device, error
    verify: ok, time (>= flash.time: it ran on what is on the board now), report, passed, failed, error
    package: ok, image_sha256 (== build.sha256.rootfs), dir, version
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import zlib
from pathlib import Path

from . import config, jobs

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STATE_DIR = ".rz-poc"
TEMPLATES = Path(__file__).resolve().parent / "templates" / "poc"
DOCS = ["README.md", "GUIDE", "image/README.md"]          # deliverable docs ("GUIDE" = docs/<guide>.md)
DESIGN = ["design/design.md", "design/verify.toml"]
INTERNAL = {"design", "build.toml", "dist", "image"}      # never copied as they are into a package
CONFIRMED_RE = re.compile(r"^\s*状态\s*[:：]\s*已确认")
LICENSED_DIRS = ("meta-rz-features",)


class PocError(Exception):
    """Bad PoC name, no PoC root configured, or no such PoC: the message says what to do."""


def root(cfg: config.Config | None = None) -> Path:
    cfg = cfg or config.load()
    if cfg.poc_root is None:
        raise PocError(f"config.toml 里没有设置 poc.root（PoC 根目录）：{cfg.path}")
    return cfg.poc_root


def repo(name: str, cfg: config.Config | None = None, must_exist: bool = True) -> Path:
    if not NAME_RE.match(name or ""):
        raise PocError(f"PoC 名称 {name!r} 不合法：只用小写字母、数字和 '-'，例如 'wdt-reboot'")
    d = root(cfg) / name
    if must_exist and not d.is_dir():
        raise PocError(f"没有 PoC {name}（{d} 不存在）。poc_status() 不带参数可以列出现有的 PoC，poc_create 新建一个。")
    return d


def state_file(name: str, cfg: config.Config | None = None) -> Path:
    return repo(name, cfg) / STATE_DIR / "state.json"


def load_state(name: str, cfg: config.Config | None = None) -> dict:
    try:
        return json.loads(state_file(name, cfg).read_text())
    except (OSError, ValueError):
        return {}


def record(name: str, key: str, data: dict, cfg: config.Config | None = None) -> None:
    """Store one tool's latest result under `key` (adds 'time' if missing); atomic replace."""
    f = state_file(name, cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    st = load_state(name, cfg)
    st[key] = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **data}
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, f)


def inputs_sha256(name: str, cfg: config.Config | None = None) -> str:
    """Hash of what a build of the PoC is made from: build.toml and every file under layers/ (path + content).
    poc_build records it with the build; poc_status compares it to tell whether the layers changed since."""
    d = repo(name, cfg)
    h = hashlib.sha256()
    files = [d / "build.toml"] + sorted(p for p in (d / "layers").rglob("*")
                                        if p.is_file() and ".git" not in p.relative_to(d).parts)
    for p in files:
        if p.is_file():
            h.update(str(p.relative_to(d)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------- helpers

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _doc_sha(p: Path, rel: str) -> str | None:
    """sha256 used for template detection; design.md without its status line (confirming is not writing)."""
    if not p.is_file():
        return None
    data = p.read_bytes()
    if rel == "design/design.md":
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    return hashlib.sha256(data).hexdigest()


def _guide(d: Path, created: dict) -> str:
    if created.get("guide"):
        return "docs/" + created["guide"]
    g = sorted((d / "docs").glob("*-Guide.md"))
    return f"docs/{g[0].name}" if g else "docs/<guide>.md"


def _unwritten(d: Path, created: dict, rels: list[str]) -> list[str]:
    """Of the given files, those missing or still exactly as poc_create wrote them."""
    tmpl, out = created.get("templates", {}), []
    for rel in rels:
        rel = _guide(d, created) if rel == "GUIDE" else rel
        sha = _doc_sha(d / rel, rel)
        if sha is None or sha == tmpl.get(rel):
            out.append(rel)
    return out


def confirmed(d: Path) -> str | None:
    """The design status line if it says 已确认, else None."""
    try:
        line = (d / "design" / "design.md").read_text(encoding="utf-8").split("\n", 1)[0]
    except OSError:
        return None
    return line.strip() if CONFIRMED_RE.match(line) else None


def _yocto_env() -> str:
    """'usable', another state of the shared Yocto project, or 'unknown' (yocto.py not available)."""
    try:
        from . import yocto
    except ImportError:
        return "unknown"
    try:
        return str(yocto.assess().get("state", "unknown"))
    except Exception as e:                   # noqa: BLE001 - reported, the agent is sent to yocto_status
        return f"error ({type(e).__name__}: {e})"


def _git(d: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(d), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_rev(d: Path) -> str | None:
    r = _git(d, "rev-parse", "HEAD")
    if not r or r.returncode:
        return None
    dirty = _git(d, "status", "--porcelain")
    return r.stdout.strip() + ("-dirty" if dirty and dirty.stdout.strip() else "")


# ---------------------------------------------------------------- poc_create

def names(name: str) -> dict:
    base = name.removeprefix("rzg3l-")
    title = " ".join(w.capitalize() for w in base.split("-"))
    return {"title": title, "guide": "RZG3L-" + "-".join(title.split()) + "-Guide.md", "layer": f"meta-rzg3l-{base}"}


def create(name: str, summary: str, cfg: config.Config | None = None) -> str:
    d = repo(name, cfg, must_exist=False)
    if d.exists():
        raise PocError(f"PoC {name} 已经存在（{d}）。调用 poc_status(name=\"{name}\") 查看它做到哪一步，接着做；"
                       f"或换一个名称新建。")
    n = names(name)
    summary = " ".join((summary or "").split())
    subst = {"{TITLE}": n["title"], "{GUIDE}": n["guide"].removesuffix(".md"), "{NAME}": name,
             "{LAYER}": n["layer"], "{SUMMARY}": summary or n["title"]}
    written: dict[str, str] = {}
    for p in sorted(TEMPLATES.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(TEMPLATES).as_posix()
        rel = {"GUIDE.md": f"docs/{n['guide']}", "gitignore": ".gitignore"}.get(rel, rel)
        if rel.startswith("layer/"):
            rel = f"layers/{n['layer']}/" + rel.removeprefix("layer/")
        out = d / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".py":
            shutil.copy2(p, out)
        else:
            text = p.read_text(encoding="utf-8")
            for k, v in subst.items():
                text = text.replace(k, v)
            out.write_text(text, encoding="utf-8")
        written[rel] = _doc_sha(out, rel)
    record(name, "created", {"summary": summary, "title": n["title"], "guide": n["guide"], "layer": n["layer"],
                             "templates": written}, cfg)
    git = "已建立 git 仓库并提交初始版本。"
    ident = _git(d, "config", "user.email")
    if _git(d, "init", "-q", "-b", "main") is None:
        git = "没有找到 git，未建立 git 仓库（不影响其他工具）。"
    else:
        who = [] if ident and ident.stdout.strip() else ["-c", "user.name=rz-mcp", "-c", "user.email=rz-mcp@localhost"]
        _git(d, "add", "-A")
        r = _git(d, *who, "commit", "-q", "-m", f"Start PoC {name} (poc_create)")
        if not r or r.returncode:
            git = "git 仓库已建立，但初始提交失败（文件已暂存）：" + ((r.stderr.strip()[-200:]) if r else "")
    return "\n".join([
        f"已创建 PoC {name}：{d}", git,
        "内部文件（不进交付包）：build.toml（镜像、可选组件、层、local.conf）、design/design.md（设计）、"
        "design/verify.toml（上板验证计划）、.rz-poc/state.json（各工具记录的状态）。",
        f"交付文件：README.md、docs/{n['guide']}、image/README.md、tools/md2pdf.py、layers/{n['layer']}/（空层）。",
        "下一步：",
        "1. yocto_status：确认共用的 Yocto 环境可用，并取得 BSP 源码（kernel、U-Boot、TF-A）的位置；不可用时 yocto_setup。",
        "2. 用文档工具（search_docs、get_section、lookup_register）和 BSP 源码调研，写 design/design.md "
        "（方案、需求对照、硬件事实及出处、改动清单、所需可选组件、风险、上板验证计划）和 design/verify.toml。",
        "3. 把设计交给用户确认（全流程唯一的决策点）。用户确认后，把 design.md 第一行改成「状态：已确认 <日期>」。",
        f"4. 之后在 layers/{n['layer']}/ 实现，poc_build(poc=\"{name}\")。随时用 poc_status(name=\"{name}\") 查看下一步。",
    ])


# ---------------------------------------------------------------- poc_status

def _short(s: str | None) -> str:
    return (s or "?")[:12]


def assess(name: str, cfg: config.Config | None = None, env: str | None = None) -> dict:
    """{"step": 0-5, "label", "tool" (next tool or None), "next" (what to do), "facts": [result lines], "done"}.
    `env`: Yocto state; asked from yocto.assess() only when the PoC still needs a build."""
    d = repo(name, cfg)
    st = load_state(name, cfg)
    created = st.get("created", {})
    b, f, v, p = (st.get(k) or {} for k in ("build", "flash", "verify", "package"))
    q = f'poc="{name}"'
    facts = []
    if b:
        facts.append(f"上一次构建（{b.get('time')}，作业 {b.get('job')}）：" + (
            f"成功，镜像 {b.get('image')}，rootfs sha256 {_short(b.get('sha256', {}).get('rootfs'))}，"
            f"文件在 {b.get('dir')}" if b.get("ok") else f"失败：{b.get('error')}（日志 {b.get('log')}）"))
    if f:
        facts.append(f"上一次写板（{f.get('time')}，作业 {f.get('job')}）：" + (
            f"成功，{f.get('parts')} → {f.get('boot_device')}，rootfs sha256 "
            f"{_short(f.get('sha256', {}).get('rootfs'))}" if f.get("ok") else f"失败：{f.get('error')}"))
    if v:
        facts.append(f"上一次验证（{v.get('time')}，作业 {v.get('job')}）：" + (
            "全部通过" if v.get("ok") else "未通过" if v.get("report") else f"未能执行：{v.get('error')}")
            + (f"，通过 {v.get('passed')} 项、失败 {v.get('failed')} 项，报告 {v.get('report')}"
               if v.get("report") else ""))
    if p:
        facts.append(f"上一次打包（{p.get('time')}，作业 {p.get('job')}）：" + (
            f"成功，{p.get('version')}，{p.get('dir')}" if p.get("ok") else f"失败：{p.get('error')}"))

    def at(step: int, label: str, tool: str | None, text: str, done: bool = False) -> dict:
        return {"step": step, "label": label, "tool": tool, "next": text, "facts": facts, "done": done, "dir": d}

    built = bool(b.get("ok")) and b.get("inputs_sha256") == inputs_sha256(name, cfg)
    if not built:
        env = env or _yocto_env()
        if env not in ("usable", "unknown"):
            return at(0, "环境", "yocto_status",
                      f"共用的 Yocto 环境不可用（{env}）：调用 yocto_status 查看原因，需要时 yocto_setup 搭建或修复。")
    design = _unwritten(d, created, DESIGN)
    if design:
        return at(1, "构想", None,
                  f"还没写完：{', '.join(design)}（仍是 poc_create 的模板或不存在）。用文档工具和 BSP 源码"
                  "（位置见 yocto_status）调研，写 design/design.md 和 design/verify.toml（上板验证计划，"
                  f"board_verify 执行它）。写完后交给用户确认。")
    if not confirmed(d):
        return at(2, "对齐", None,
                  "设计还没有得到用户确认。把 design/design.md 的内容（方案、改动清单、可选组件、风险、验证计划）"
                  "交给用户，请用户确认或提修改意见；用户确认后，把 design.md 第一行改成「状态：已确认 "
                  f"{time.strftime('%Y-%m-%d')}」。这是全流程唯一需要用户决定的地方，不要替用户确认。")
    fix = f"按设计在 layers/ 里实现（改动后层的内容就变了），然后 poc_build({q})。"
    if not b:
        return at(3, "实现和构建", "poc_build", "还没有构建。" + fix)
    if not b.get("ok"):
        return at(3, "实现和构建", "poc_build", f"上一次构建失败：{b.get('error')}。看日志 {b.get('log')}，"
                  f"修改 layers/ 或 build.toml 后再次 poc_build({q})。")
    if not built:
        return at(3, "实现和构建", "poc_build", "上一次构建之后 layers/ 或 build.toml 改过了，镜像已经过时："
                  f"重新 poc_build({q})。")
    bs = b.get("sha256", {})
    on_board = f.get("ok") and f.get("sha256", {}).get("rootfs") == bs.get("rootfs")
    if not on_board:
        boot = {bs.get("fip"), bs.get("fip_bin")} - {None}
        parts = "rootfs" if f.get("ok") and boot & set(f.get("sha256", {}).values()) else "all"
        dev = f.get("boot_device") or "emmc"
        why = (f"上一次写板失败：{f.get('error')}。" if f and not f.get("ok") else
               "板子上的不是当前这次构建的镜像。" if f else "当前构建还没有写进板子。")
        return at(4, "上板", "board_flash", why + f"调用 board_flash(parts=\"{parts}\", boot_device=\"{dev}\", {q})"
                  "（它自己检查主机和板子，并给出需要用户做的物理操作）。")
    if not v or (v.get("time") or "") < (f.get("time") or ""):
        return at(4, "上板", "board_verify", f"镜像已写进板子，还没有按验证计划验证：board_verify(plan=\"{name}\")。")
    if not v.get("ok"):
        if not v.get("report"):
            return at(4, "上板", "board_verify", f"上一次验证没能执行：{v.get('error')}。处理后再次 "
                      f"board_verify(plan=\"{name}\")。")
        return at(3, "实现和构建", "poc_build",
                  f"验证未通过（失败 {v.get('failed')} 项），报告 {v.get('report')}。按报告找原因，" + fix +
                  f" 如果是验证计划本身写错了，改 design/verify.toml 后直接 board_verify(plan=\"{name}\")。")
    docs = _unwritten(d, created, DOCS)
    if docs:
        return at(5, "交付", None, f"验证已通过。还要写交付文档：{', '.join(docs)}（仍是模板或不存在）。"
                  "依据 design/design.md 和验证报告写，删除模板里的注释；写完后 poc_package。")
    if not (p.get("ok") and p.get("image_sha256") == bs.get("rootfs")):
        return at(5, "交付", "poc_package", f"文档已写好。调用 poc_package({q}, version=\"v1.0\") 生成交付包。")
    return at(5, "交付", None, f"已完成：交付包在 {p.get('dir')}。如果之后又改了文档，再次 poc_package 会重新生成。",
              done=True)


def status_text(name: str | None = None, cfg: config.Config | None = None) -> str:
    cfg = cfg or config.load()
    if name:
        a = assess(name, cfg)
        lines = [f"PoC {name}：{a['dir']}",
                 f"当前：第 {a['step']} 步 {a['label']}" + ("（已完成）" if a["done"] else "")]
        lines += a["facts"] or ["还没有构建、写板或验证的记录。"]
        lines.append(("下一步（调用 " + a["tool"] + "）：" if a["tool"] else "下一步：") + a["next"])
        return "\n".join(lines)
    r = root(cfg)
    items = sorted(p.name for p in r.iterdir() if p.is_dir() and NAME_RE.match(p.name)
                   and ((p / STATE_DIR).is_dir() or (p / "build.toml").is_file())) if r.is_dir() else []
    if not items:
        return f"{r} 下还没有 PoC。用 poc_create 新建一个。"
    env = _yocto_env()
    lines = [f"PoC 根目录 {r}（Yocto 环境：{env}）："]
    for n in items:
        try:
            a = assess(n, cfg, env)
            lines.append(f"- {n}：第 {a['step']} 步 {a['label']}" + ("，已完成" if a["done"] else
                         f"，下一步 {a['tool'] or '由 agent 处理（见 poc_status）'}"))
        except Exception as e:                # noqa: BLE001 - one broken PoC must not hide the others
            lines.append(f"- {n}：无法判断（{e}）")
    lines.append("用 poc_status(name=...) 查看某个 PoC 的详情和下一步。")
    return "\n".join(lines)


# ---------------------------------------------------------------- poc_package

def package_problems(name: str, cfg: config.Config | None = None) -> list[str]:
    """Why the PoC cannot be packaged yet (empty list: it can)."""
    d = repo(name, cfg)
    st = load_state(name, cfg)
    created, b, out = st.get("created", {}), st.get("build") or {}, []
    docs = _unwritten(d, created, DOCS)
    if docs:
        out.append(f"交付文档还没写：{', '.join(docs)}（仍是模板或不存在）")
    tmpl = created.get("templates", {})
    layer_files = [p for p in (d / "layers").rglob("*") if p.is_file() and ".git" not in p.parts] \
        if (d / "layers").is_dir() else []
    if not any((p / "conf" / "layer.conf").is_file() for p in (d / "layers").glob("*")) or \
            all(_doc_sha(p, p.relative_to(d).as_posix()) == tmpl.get(p.relative_to(d).as_posix())
                for p in layer_files):
        out.append("layers/ 里没有实现（只有 poc_create 生成的空层）")
    if not b.get("ok"):
        out.append(f"没有成功的构建：先 poc_build(poc=\"{name}\")")
        return out
    for k, sha in (b.get("sha256") or {}).items():
        path = Path((b.get("files") or {}).get(k, ""))
        if not path.is_file() or sha256_file(path) != sha:
            out.append(f"构建记录里的文件 {k}（{path}）不存在或已经变了：重新 poc_build(poc=\"{name}\")")
    if "rootfs" not in (b.get("files") or {}):
        out.append("构建记录里没有 rootfs 镜像")
    if b.get("inputs_sha256") != inputs_sha256(name, cfg):
        out.append(f"上一次构建之后 layers/ 或 build.toml 改过了，镜像与层不一致：重新 poc_build(poc=\"{name}\")")
    return out


def package_start(name: str, version: str, cfg: config.Config | None = None) -> str:
    if not VERSION_RE.match(version or ""):
        raise PocError(f"版本 {version!r} 不合法：只用字母、数字、'.'、'_'、'-'，例如 'v1.0'")
    problems = package_problems(name, cfg)
    if problems:
        return f"PoC {name} 还不能打包，什么都没有写：\n" + "\n".join(f"- {x}" for x in problems) + \
            f"\n用 poc_status(name=\"{name}\") 查看下一步。"
    try:
        st = jobs.start("poc_package", {"name": name, "version": version}, title=f"打包 PoC {name} {version}",
                        steps=["检查输入", "复制交付文件并检查授权内容", "生成 PDF", "写版本清单和校验和"],
                        poc=name, lock=f"poc-package-{name}")
    except jobs.Busy as e:
        return f"{e}：同一个 PoC 的打包正在进行，用 job_status 跟踪它。"
    return jobs.format_started(st)


def _licensed(cfg: config.Config) -> tuple[dict, dict]:
    """sha256 of each licensed zip, and (size, crc32) -> member name of the files inside them."""
    zips, members = {}, {}
    import zipfile
    for z in sorted(cfg.packages.rglob("*.zip")) if cfg.packages.is_dir() else []:
        zips[sha256_file(z)] = z.name
        try:
            for i in zipfile.ZipFile(z).infolist():
                if i.file_size > 64 and not i.is_dir():
                    members[(i.file_size, i.CRC)] = f"{z.name}:{i.filename}"
        except zipfile.BadZipFile:
            pass
    return zips, members


def _package_job(ctx) -> dict:
    name, version = ctx.params["name"], ctx.params["version"]
    try:
        return _package(ctx, name, version)
    except jobs.JobError as e:
        record(name, "package", {"ok": False, "job": ctx.job["id"], "version": version, "error": str(e)})
        raise


def _package(ctx, name: str, version: str) -> dict:
    cfg = config.load()
    ctx.step(1)
    problems = package_problems(name, cfg)
    if problems:
        raise jobs.JobError("还不能打包：" + "；".join(problems), f"poc_status(name=\"{name}\") 给出下一步。")
    d = repo(name, cfg)
    b = load_state(name, cfg)["build"]
    missing = [t for t in ("firefox", "gs") if not shutil.which(t)]
    if missing:
        raise jobs.JobError(f"生成 PDF 需要 {' 和 '.join(missing)}，主机上没有找到",
                            "安装 firefox 和 ghostscript 后重新调用 poc_package。")
    pkg = f"{name}-{version}"
    final, tmp = d / "dist" / pkg, d / "dist" / f".{pkg}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        ctx.step(2)
        tmp.mkdir(parents=True)
        skip = shutil.ignore_patterns(".*", "__pycache__", "*.pyc")
        for item in sorted(d.iterdir()):
            if item.name.startswith(".") or item.name in INTERNAL:
                continue
            (shutil.copytree(item, tmp / item.name, ignore=skip) if item.is_dir()
             else shutil.copy2(item, tmp / item.name))
        (tmp / "image").mkdir()
        if (d / "image" / "README.md").is_file():
            shutil.copy2(d / "image" / "README.md", tmp / "image" / "README.md")
        rootfs = Path(b["files"]["rootfs"])
        plain = re.sub(r"\.rootfs-\d{14}\.", ".rootfs.", rootfs.name)
        ctx.progress(f"复制镜像 {rootfs.name}")
        shutil.copyfile(rootfs, tmp / "image" / plain)
        if b["files"].get("bmap"):
            shutil.copyfile(b["files"]["bmap"], tmp / "image" / plain.replace(".wic.gz", ".wic.bmap"))

        zips, members = _licensed(cfg)
        sums, bad = {}, []
        for p in sorted(x for x in tmp.rglob("*") if x.is_file()):
            rel = p.relative_to(tmp).as_posix()
            if any(part.startswith(LICENSED_DIRS) for part in p.relative_to(tmp).parts):
                bad.append(f"{rel}（授权软件包的层）")
            h, crc = hashlib.sha256(), 0
            with open(p, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(blk)
                    crc = zlib.crc32(blk, crc)
            sums[rel] = h.hexdigest()
            hit = zips.get(sums[rel]) or members.get((p.stat().st_size, crc))
            if hit:
                bad.append(f"{rel}（等于授权软件包 {hit}）")
        if bad:
            raise jobs.JobError("交付内容里有授权软件包的内容，已停止，没有生成交付包：" + "；".join(bad),
                                "从 PoC 仓库里删除这些文件（授权软件包只能由用户从 renesas.com 获取），然后重新 poc_package。")

        ctx.step(3)
        md2pdf = tmp / "tools" / "md2pdf.py"
        md2pdf = md2pdf if md2pdf.is_file() else TEMPLATES / "tools" / "md2pdf.py"
        for md in sorted((tmp / "docs").glob("*.md")) if (tmp / "docs").is_dir() else []:
            ctx.progress(f"渲染 {md.name}")
            try:
                r = subprocess.run([sys.executable, str(md2pdf), str(md), str(md.with_suffix(".pdf"))],
                                   cwd=md.parent, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                raise jobs.JobError(f"md2pdf 渲染 {md.name} 超过 600 秒", "确认 firefox 能以 headless 方式启动后重试。")
            if r.returncode or not md.with_suffix(".pdf").is_file():
                ctx.log(r.stdout + r.stderr)
                raise jobs.JobError(f"md2pdf 没能生成 {md.name} 的 PDF：{(r.stderr or r.stdout).strip()[-400:]}",
                                    "完整输出在作业日志里。修正文档或 firefox / gs 后重新 poc_package。")
            ctx.log(f"PDF: {md.with_suffix('.pdf').name}", key=True)

        ctx.step(4)
        for p in sorted(x for x in tmp.rglob("*") if x.is_file()):
            rel = p.relative_to(tmp).as_posix()
            if rel not in sums or p.suffix == ".pdf":
                sums[rel] = sha256_file(p)
        bsp: object = "unknown"
        try:
            from . import yocto
            bsp = yocto.release()
        except Exception:                     # noqa: BLE001 - P3 not available: recorded as unknown
            pass
        bs = b.get("sha256", {})
        versions = {
            "package": pkg, "version": version, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "poc": {"name": name, "git": _git_rev(d)},
            "bsp": bsp,
            "build": {k: b.get(k) for k in ("time", "job", "image", "layers", "conf_sha256", "inputs_sha256")},
            "image": {"file": f"image/{plain}", "sha256": bs.get("rootfs"), "bmap_sha256": bs.get("bmap")},
            "boot_files": {k: {"file": Path(b["files"][k]).name, "sha256": v} for k, v in bs.items()
                           if k not in ("rootfs", "bmap") and k in b.get("files", {})},
        }
        (tmp / "versions.json").write_text(json.dumps(versions, ensure_ascii=False, indent=1, default=str) + "\n")
        sums["versions.json"] = sha256_file(tmp / "versions.json")
        (tmp / "SHA256SUMS").write_text("".join(f"{s}  {r}\n" for r, s in sorted(sums.items())))
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    record(name, "package", {"ok": True, "job": ctx.job["id"], "dir": str(final), "version": version,
                             "image_sha256": bs.get("rootfs")})
    files = len(sums) + 1
    ctx.report(f"# 交付包 {pkg}\n\n- 目录：{final}\n- 文件：{files} 个（清单见 SHA256SUMS）\n"
               f"- 镜像：image/{plain}，sha256 {bs.get('rootfs')}\n")
    verify = load_state(name, cfg).get("verify") or {}
    note = "" if verify.get("ok") else "（注意：状态里没有通过的上板验证记录）"
    return {"summary": f"交付包已生成：{final}（{files} 个文件）{note}", "dir": str(final),
            "next": "把交付包的位置告诉用户。改了文档后再次 poc_package 会替换同一版本的交付包。"}
