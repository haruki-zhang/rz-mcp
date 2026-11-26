#!/usr/bin/env python3
"""Prototype 3: the index with the Linux Start-up Guide added, built into a scratch database (data/rz.db untouched).

    PYTHONPATH=src python3 eval/gap/proto/sug.py <out.db>
"""
import sys
from pathlib import Path

from rzmcp import build_fulltext, build_registers, catalog

SUG = {"id": "R01US0858EJ0100", "title": "RZ/G3L Linux Start-up Guide (Rev.1.00-rc1)", "doc_type": "quick_start",
       "rev": "1.00-rc1", "path": "/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1/r01us0858ej0100-rzg3l(Linux Start-up_Guide_RZG3L)_rc1.pdf",
       "pages": 48, "devices": ["rzg3l"], "boards": ["rzg3l-evk"], "confidentiality": "public", "index": ["fulltext"],
       "outline": "bookmarks", "note": "prototype 3"}

_load = catalog.load


def load(*a, **k):
    m = _load(*a, **k)
    m["documents"] = m["documents"] + [SUG]
    return m


catalog.load = load
build_fulltext.catalog.load = load
build_registers.catalog.load = load
build_fulltext.build(Path(sys.argv[1]))
build_registers.build(Path(sys.argv[1]))
