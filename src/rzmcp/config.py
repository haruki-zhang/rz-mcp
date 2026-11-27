"""Machine configuration: config.toml at the repository root (template: config.example.toml).

Every host-specific value (Yocto project, build container, serial port, network adapter, TFTP root, PoC root)
comes from here. A missing file or key is not an error at load time: the tool that needs the value reports
which key to set.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import ROOT

# RZ_MCP_CONFIG points tests (and their job runners, which inherit the environment) at another config file
CONFIG_PATH = Path(os.environ.get("RZ_MCP_CONFIG") or ROOT / "config.toml")
JOBS_DIR = ROOT / "data" / "jobs"


@dataclass(frozen=True)
class Config:
    path: Path
    exists: bool
    yocto_project: Path | None = None
    container: str | None = None
    default_image: str = "core-image-minimal"
    packages: Path = ROOT / "packages"
    poc_root: Path | None = None
    serial: str | None = None
    baud: int = 115200
    nic: str | None = None
    host_ip: str = "192.168.50.1"
    board_ip: str = "192.168.50.2"
    tftp_root: Path | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def missing(self, *keys: str) -> list[str]:
        """Names (as written in config.toml, e.g. 'board.serial') of the given keys that are not set."""
        attr = {"yocto.project": "yocto_project", "yocto.container": "container", "poc.root": "poc_root",
                "board.serial": "serial", "board.nic": "nic", "board.tftp_root": "tftp_root"}
        return [k for k in keys if getattr(self, attr[k]) is None]


def _path(v: str | None) -> Path | None:
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.is_absolute() else ROOT / p


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        return Config(path=path, exists=False)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    y, p, b = raw.get("yocto", {}), raw.get("poc", {}), raw.get("board", {})
    defaults = Config(path=path, exists=True)
    return Config(
        path=path, exists=True, raw=raw,
        yocto_project=_path(y.get("project")),
        container=y.get("container") or None,
        default_image=y.get("default_image") or defaults.default_image,
        packages=_path(y.get("packages")) or defaults.packages,
        poc_root=_path(p.get("root")),
        serial=b.get("serial") or None,
        baud=int(b.get("baud", defaults.baud)),
        nic=b.get("nic") or None,
        host_ip=b.get("host_ip") or defaults.host_ip,
        board_ip=b.get("board_ip") or defaults.board_ip,
        tftp_root=_path(b.get("tftp_root")),
    )
