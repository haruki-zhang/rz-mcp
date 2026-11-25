"""Yocto project helpers shared by the MCP tools (rzmcp.bsp) and the build scripts (workflow/build).

Only reads files; nothing here runs bitbake.
"""
import fcntl
import os
import re
import tomllib
from pathlib import Path

from . import ROOT

RELEASES = ROOT / "catalog" / "bsp_releases.toml"
DEFAULT_RELEASE = "RZG3L-BSP-1.0.0"


def project_dir() -> Path:
    return Path(os.environ.get("RZ_POC_PROJECT", ROOT.parent / "rzg3l-bsp-v1"))


def release(name: str = DEFAULT_RELEASE) -> dict:
    rels = tomllib.loads(RELEASES.read_text())["releases"]
    if name not in rels:
        raise KeyError(f"unknown release {name!r}; known: {', '.join(rels)}")
    return rels[name]


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
