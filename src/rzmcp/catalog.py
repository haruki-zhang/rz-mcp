"""Load and sanity-check catalog/manifest.toml."""
from __future__ import annotations

import tomllib
from pathlib import Path

from . import MANIFEST, ROOT

DOC_TYPES = {"hw_manual", "datasheet", "app_note", "quick_start", "board_manual", "schematic", "bom",
             "errata", "sw_manual", "component_datasheet"}


def load(path: Path = MANIFEST) -> dict:
    with open(path, "rb") as f:
        m = tomllib.load(f)
    errors = []
    seen = set()
    for d in m.get("documents", []):
        if d["id"] in seen:
            errors.append(f"duplicate id {d['id']}")
        seen.add(d["id"])
        if d["doc_type"] not in DOC_TYPES:
            errors.append(f"{d['id']}: unknown doc_type {d['doc_type']}")
        if not (ROOT / d["path"]).exists():
            errors.append(f"{d['id']}: missing file {d['path']}")
    if errors:
        raise ValueError("manifest errors:\n  " + "\n  ".join(errors))
    return m
