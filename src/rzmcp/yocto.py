"""Yocto project helpers shared by the MCP tools (rzmcp.bsp), the poc prompt (rzmcp.workflow) and the build
scripts (workflow/build).

Only reads: files, `git` metadata and `docker inspect`. Nothing here runs bitbake or changes a project; setting a
project up is workflow/build/yocto-project.
"""
import fcntl
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

from . import ROOT

RELEASES = ROOT / "catalog" / "bsp_releases.toml"
DEFAULT_RELEASE = "RZG3L-BSP-1.0.0"
CONTAINER = "max-ubuntu22.04"            # the build container (Ubuntu 22.04); it must see the project at the same path
STATE_DIR = ".rz-mcp"                    # inside a project: project.json (what yocto-project did), setup.lock, staging
# Entries that do not make a directory "not empty" for the purpose of setting a project up in it
IGNORABLE = {".claude", ".vscode", STATE_DIR}


def default_project() -> Path:
    return Path(os.environ.get("RZ_POC_PROJECT", ROOT.parent / "rzg3l-bsp-v1"))


def project_dir() -> Path:
    return default_project()


def resolve_project(arg: str | Path | None) -> Path:
    """An explicit project directory (absolute, '~' allowed) or the default one. Need not exist."""
    if arg is None or str(arg).strip() == "":
        return default_project()
    p = Path(str(arg).strip()).expanduser()
    if not p.is_absolute():
        raise ValueError(f"project directory must be an absolute path: {arg!r}")
    return Path(os.path.normpath(p))


def release(name: str = DEFAULT_RELEASE) -> dict:
    rels = tomllib.loads(RELEASES.read_text())["releases"]
    if name not in rels:
        raise KeyError(f"unknown release {name!r}; known: {', '.join(rels)}")
    return rels[name]


def optional_components(rel: dict | None = None) -> list[dict]:
    return list((rel or release()).get("optional", []))


def component_for_layer(layer: str, rel: dict | None = None) -> dict | None:
    """The optional component that provides a layer path (relative to the project), if any."""
    layer = layer.strip().rstrip("/")
    return next((c for c in optional_components(rel) if layer in c["layers"] + c.get("legacy_layers", [])), None)


# ---------------------------------------------------------------- conf files

def statements(text: str) -> list[str]:
    """BitBake conf statements without comments and blank lines; backslash continuations joined, whitespace
    normalised. Two files with the same statements in the same order configure the same thing."""
    out, cur = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not cur and (not line.strip() or line.lstrip().startswith("#")):
            continue
        cur += line[:-1] + " " if line.endswith("\\") else line
        if line.endswith("\\"):
            continue
        out.append(re.sub(r"\s+", " ", cur).strip())
        cur = ""
    if cur:
        out.append(re.sub(r"\s+", " ", cur).strip())
    return out


def bblayers(path: Path, build: Path) -> list[Path]:
    """Layer directories in BBLAYERS, resolved (${TOPDIR} = the build directory)."""
    text = " ".join(statements(path.read_text()))
    m = re.search(r'BBLAYERS\s*\??\??=\s*"([^"]*)"', text)
    if not m:
        return []
    return [Path(p.replace("${TOPDIR}", str(build))).resolve() for p in m.group(1).split()]


def conf_value(stmts: list[str], var: str) -> str | None:
    """The value a simple sequence of assignments leaves (=, ?=, ??=, +=, :append); good enough for MACHINE,
    DISTRO, RM_WORK_EXCLUDE, DL_DIR in local.conf."""
    val = None
    for s in stmts:
        m = re.match(rf'{re.escape(var)}(:append)?\s*(\?\?=|\?=|\+=|=)\s*"(.*)"$', s)
        if not m:
            continue
        op, v = m.group(2), m.group(3)
        if m.group(1):
            val = (val or "") + v
        elif op == "+=":
            val = ((val or "") + " " + v).strip()
        elif op == "=":
            val = v
        elif val is None:
            val = v
    return val


def recipe_var(path: Path, var: str, override: str = "rzg3l-family") -> str | None:
    """VAR:<override> if the recipe sets it, else VAR."""
    text = path.read_text()
    for name in (f"{var}:{override}", var):
        m = re.search(rf'^{re.escape(name)}\s*\??=\s*"([^"]*)"', text, re.M)
        if m:
            return m.group(1)
    return None


def bitbake_busy(build: Path) -> bool:
    """True if a bitbake server holds build/bitbake.lock (bitbake uses POSIX record locks on it)."""
    lock = build / "bitbake.lock"
    if not lock.exists():
        return False
    try:
        with open(lock, "a") as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def dl_dir(project: Path) -> Path:
    stmts = statements((project / "build" / "conf" / "local.conf").read_text())
    v = conf_value(stmts, "DL_DIR")
    return Path(v.replace("${TOPDIR}", str(project / "build"))) if v else project / "build" / "downloads"


def owner_of(layer: Path, project: Path) -> str:
    """Which part of the Yocto project a layer belongs to: a PoC repository, the BSP, or a feature package."""
    try:
        rel = layer.relative_to(project)
    except ValueError:
        return "outside the project"
    top = rel.parts[0]
    if top in ("poky", "meta-openembedded", "meta-arm", "meta-renesas"):
        return f"BSP ({top})"
    if top == "meta-rz-features":
        return "Renesas feature package (meta-rz-features)"
    if (project / top / ".git").exists() and (project / top) != layer:
        return f"PoC repository {top}"
    return f"project layer {top}"


# ---------------------------------------------------------------- project state (is it a usable Yocto project?)

def state_file(project: Path) -> Path:
    return project / STATE_DIR / "project.json"


def read_state(project: Path) -> dict | None:
    f = state_file(project)
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except (OSError, ValueError):
        return None


def setup_running(project: Path) -> bool:
    """True while workflow/build/yocto-project holds <project>/.rz-mcp/setup.lock."""
    lock = project / STATE_DIR / "setup.lock"
    if not lock.exists():
        return False
    try:
        with open(lock, "a") as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def container_sees(path: Path, container: str = CONTAINER) -> bool | None:
    """Whether the build container bind-mounts `path` at the same path (None: docker not available)."""
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{json .Mounts}}", container], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for m in json.loads(r.stdout or "[]"):
        if m.get("Source") == m.get("Destination") and Path(path).is_relative_to(m["Source"]):
            return True
    return False


def _is_poc_repo(p: Path) -> bool:
    return p.is_dir() and (p / "design").is_dir() and (p / ".git").exists()


def _git_head(d: Path) -> str | None:
    if not (d / ".git").exists():
        return None
    r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def mirror_has(project: Path, mirror: str, rev: str) -> bool:
    m = dl_dir(project) / "git2" / mirror if (project / "build" / "conf" / "local.conf").exists() \
        else project / "build" / "downloads" / "git2" / mirror
    return m.exists() and subprocess.run(["git", f"--git-dir={m}", "cat-file", "-e", f"{rev}^{{commit}}"],
                                         capture_output=True).returncode == 0


def assess(project: Path, rel_name: str = DEFAULT_RELEASE, as_finished: bool = False) -> dict:
    """Classify a directory as a Yocto project for the release:

    absent      does not exist, or holds nothing but IGNORABLE entries and PoC repositories: set it up
    incomplete  a setup by yocto-project that has not finished (resumable; `running` if it is going on now)
    usable      the release's layers are there and match it: reuse as it is (`warnings` are informational)
    unusable    anything else (`problems` says why): never touch it

    as_finished=True checks an unfinished setup as if it were done (yocto-project's own check before it marks it so).
    """
    rel = release(rel_name)
    out = {"project": str(project), "release": rel_name, "state": None, "problems": [], "warnings": [],
           "components": {}, "running": setup_running(project), "setup": read_state(project)}
    sees = container_sees(project)
    if sees is False:
        out["problems"].append(f"the build container {CONTAINER} does not mount {project} (it must be under a "
                               "directory the container bind-mounts at the same path, e.g. /home/mpu-epmaj/max)")
    elif sees is None:
        out["warnings"].append(f"docker / the container {CONTAINER} is not available: could not check that it sees the project")
    if not project.exists():
        out["state"] = "absent" if not out["problems"] else "unusable"
        return out
    if not project.is_dir():
        out["problems"].append("not a directory")
        out["state"] = "unusable"
        return out
    st = out["setup"] or {}
    if st.get("created_by") == "yocto-project" and not st.get("done") and not as_finished:
        out["state"] = "unusable" if out["problems"] else "incomplete"
        return out
    entries = [e for e in project.iterdir() if e.name not in IGNORABLE and not _is_poc_repo(e)]
    if not entries:
        out["state"] = "absent" if not out["problems"] else "unusable"
        return out

    # the release's layers: the official template's BBLAYERS, plus the template itself
    tmpl = project / rel["template"]
    if not (project / "poky" / "oe-init-build-env").exists():
        out["problems"].append("no poky/oe-init-build-env")
    if not tmpl.is_dir():
        out["problems"].append(f"no {rel['template']} (the release's TEMPLATECONF)")
    else:
        for l in bblayers(tmpl / "bblayers.conf.sample", project / "build"):
            if not (l / "conf" / "layer.conf").exists():
                out["problems"].append(f"layer of the official template missing: {l.relative_to(project) if l.is_relative_to(project) else l}")
    if out["problems"]:
        names = ", ".join(sorted(e.name for e in entries)[:8])
        out["problems"].insert(0, f"not empty and not a {rel_name} Yocto project (it holds: {names}"
                               f"{', ...' if len(entries) > 8 else ''})")
        out["state"] = "unusable"
        return out

    # which release: git checkouts by revision, tarballs by content
    for l in rel["layers"]:
        head = _git_head(project / l["name"])
        if head is None:
            out["warnings"].append(f"{l['name']} is not a git checkout (from a tarball?): its revision is checked by content only")
        elif head != l["rev"]:
            out["warnings"].append(f"{l['name']} is at {head[:12] or '?'}, the release pins {l['rev'][:12]}")
    pv = project / "poky" / "meta-poky" / "conf" / "distro" / "poky.conf"
    want_pv = rel.get("poky_version")
    got_pv = re.search(r'^DISTRO_VERSION\s*=\s*"([^"]+)"', pv.read_text(), re.M) if pv.exists() else None
    if want_pv and (not got_pv or got_pv.group(1) != want_pv):
        out["problems"].append(f"poky DISTRO_VERSION {got_pv.group(1) if got_pv else '?'} != {want_pv} of {rel_name}")
    for n, c in rel["components"].items():
        recipe = project / "meta-renesas" / c["recipe"]
        pin = recipe_var(recipe, c["rev_var"]) if recipe.exists() else None
        if pin != c["rev"]:
            out["problems"].append(f"meta-renesas pins {n} {pin and pin[:12]}, {rel_name} pins {c['rev'][:12]} (a different BSP release?)")
        elif not mirror_has(project, c["mirror"], c["rev"]):
            out["warnings"].append(f"the official {n} source is not fetched yet (bsp_source / bsp_grep need it): "
                                   "yocto-project setup fetches it")
    if not (project / "build" / "conf" / "bblayers.conf").exists():
        out["warnings"].append("no build/conf yet: yocto-project setup writes the baseline (poc-use --baseline)")
    recorded = (out["setup"] or {}).get("components", {})
    for c in optional_components(rel):
        present = [l for l in c["layers"] + c.get("legacy_layers", []) if (project / l / "conf" / "layer.conf").exists()]
        if present:
            r = recorded.get(c["name"])
            out["components"][c["name"]] = (f"{c.get('version') or c.get('tag')} (installed by yocto-project {r['installed']})"
                                            if r else "present (not installed by yocto-project: version not checked)")
    if out["problems"]:
        out["problems"].insert(0, f"a Yocto project, but not usable as {rel_name}")
    out["state"] = "unusable" if out["problems"] else "usable"
    return out


def describe(a: dict) -> str:
    """One paragraph for people and prompts."""
    head = {"absent": "不存在（或是空目录）：可以自动搭建", "incomplete": "上次的搭建没有完成" + ("，正在进行中" if a["running"] else "：可以接着搭"),
            "usable": "可用：直接复用", "unusable": "不可用：不要改动它"}[a["state"]]
    lines = [f"{a['project']}：{head}"]
    lines += [f"  - 问题：{p}" for p in a["problems"]]
    lines += [f"  - 提醒：{w}" for w in a["warnings"]]
    if a["components"]:
        lines.append("  - 可选组件：" + "；".join(f"{k} {v}" for k, v in a["components"].items()))
    return "\n".join(lines)


# ---------------------------------------------------------------- which project a PoC lives in

def registry_file() -> Path:
    return Path(os.environ.get("RZ_POC_REGISTRY", ROOT / "data" / "poc-projects.json"))


def registry() -> dict:
    try:
        return json.loads(registry_file().read_text())
    except (OSError, ValueError):
        return {}


def register_poc(name: str, project: Path) -> None:
    reg = registry()
    if reg.get(name) == str(project):
        return
    reg[name] = str(project)
    f = registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1, ensure_ascii=False))
    tmp.replace(f)
