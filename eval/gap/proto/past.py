"""Prototype 2 (not wired into the server): read-only access to past PoCs and past board logs, with provenance.

The registered sources are the delivered PoC repositories and the logs kept in the shared project. A git
repository is read at its HEAD commit (repo@commit:path); anything else as on disk. This is the light form of B1b:
no index, the same read / grep the agents did with Bash, but citable and available to a session in another project.
"""
import subprocess
from pathlib import Path

SHARED = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1")
SOURCES = ["rzg3l-fast-boot-seamless-logo", "rzg3l-fast-boot-seamless-logo-standalone", "rzg3l-lowpower-display",
           "MultiCore-Display", "meta-rzg3l-combo", "logs"]


def resolve(path: str) -> tuple[Path, str, str | None] | None:
    """(source dir, path inside it, HEAD commit or None) for 'rzg3l-lowpower-display/docs/...'."""
    top = path.split("/", 1)[0]
    if top not in SOURCES:
        return None
    d = SHARED / top
    head = None
    if (d / ".git").exists():
        head = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return d, path.split("/", 1)[1] if "/" in path else "", head


def past_source(path: str) -> str | None:
    r = resolve(path)
    if not r:
        return None
    d, rel, head = r
    if head:
        p = subprocess.run(["git", "-C", str(d), "show", f"{head}:{rel}"], capture_output=True, text=True, errors="replace")
        if p.returncode == 0:
            return p.stdout
    f = d / rel
    return f.read_text(errors="replace") if f.is_file() else None
