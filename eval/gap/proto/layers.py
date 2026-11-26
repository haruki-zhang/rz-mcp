"""Prototype 1 (not wired into the server): official BSP layer metadata and other components' sources.

layer_source / layer_grep read the release's layers (poky, meta-arm, meta-openembedded, meta-renesas) and the
optional components (meta-rz-features) of a Yocto project. A layer that is a git checkout is read at the
revision the release pins (git show / git grep), so edits in the working tree never pass for official content;
a tarball layer (rzg3l-bsp-v1) is read from disk and says so.
recipe_source reads another component's official source (e.g. systemd) from DL_DIR's git mirror at the SRCREV
its recipe sets, found by parsing the recipe (.bb plus the .inc files it requires).
"""
import re
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from rzmcp import yocto as Y  # noqa: E402

LAYERS = ("poky", "meta-arm", "meta-openembedded", "meta-renesas", "meta-rz-features")


def _rev(project: Path, top: str) -> str | None:
    pins = {l["name"]: l["rev"] for l in Y.release()["layers"]}
    if top in pins and (project / top / ".git").exists():
        return pins[top]
    return None


def layer_source(project: Path, path: str, start: int = 1, max_lines: int = 300) -> str:
    top = path.split("/", 1)[0]
    if top not in LAYERS:
        raise ValueError(f"not a BSP layer path: {path}")
    rev = _rev(project, top)
    if rev:
        rel = path.split("/", 1)[1] if "/" in path else ""
        r = subprocess.run(["git", "-C", str(project / top), "show", f"{rev}:{rel}"], capture_output=True, text=True)
        if r.returncode:
            return f"{path} @ {rev[:12]}: not found"
        text, where = r.stdout, f"{top}@{rev[:12]}"
    else:
        f = project / path
        if not f.is_file():
            return f"{path}: not found"
        text, where = f.read_text(errors="replace"), f"{top} (not a git checkout: as on disk)"
    lines = text.splitlines()
    end = min(len(lines), start + max_lines - 1)
    return f"{where}:{path.split('/', 1)[1] if '/' in path else ''}  lines {start}-{end} of {len(lines)}\n" + \
        "\n".join(lines[start - 1:end])


def layer_grep(project: Path, pattern: str, path: str) -> list[str]:
    top = path.split("/", 1)[0]
    rev = _rev(project, top)
    rel = path.split("/", 1)[1] if "/" in path else ""
    if rev:
        r = subprocess.run(["git", "-C", str(project / top), "grep", "-n", "-I", "-E", "-e", pattern, rev, "--", rel or "."],
                           capture_output=True, text=True)
        return [f"{top}/{h.split(':', 1)[1]}" for h in r.stdout.splitlines()]
    r = subprocess.run(["grep", "-rnIE", "-e", pattern, str(project / path)], capture_output=True, text=True)
    return [h.replace(str(project) + "/", "") for h in r.stdout.splitlines()]


def recipe_files(project: Path, recipe: str) -> list[Path]:
    out = []
    for top in LAYERS:
        out += sorted((project / top).rglob(f"{recipe}_*.bb")) + sorted((project / top).rglob(f"{recipe}.bb"))
    return out


def recipe_mirror(project: Path, recipe: str) -> tuple[str, str] | None:
    """(git mirror directory name, SRCREV) from the recipe and the .inc files it requires."""
    texts = []
    for bb in recipe_files(project, recipe):
        t = bb.read_text(errors="replace")
        texts.append(t)
        for inc in re.findall(r"^(?:require|include)\s+(\S+\.inc)", t, re.M):
            p = bb.parent / inc
            if p.exists():
                texts.append(p.read_text(errors="replace"))
    t = "\n".join(texts)
    rev = re.search(r'^SRCREV\s*\??=\s*"([0-9a-f]{40})"', t, re.M)
    uri = re.search(r'git://([^;"\s]+?)(?:\.git)?;', t)
    if not (rev and uri):
        return None
    return uri.group(1).replace("/", ".") + ".git", rev.group(1)


def recipe_source(project: Path, recipe: str, path: str) -> str:
    m = recipe_mirror(project, recipe)
    if not m:
        return f"{recipe}: no git SRC_URI / SRCREV found"
    mirror, rev = m
    git2 = Y.dl_dir(project) / "git2" / mirror
    r = subprocess.run(["git", f"--git-dir={git2}", "show", f"{rev}:{path}"], capture_output=True, text=True)
    return f"{recipe} @ {rev[:12]} ({mirror}): {path}\n" + (r.stdout if r.returncode == 0 else "not found")
