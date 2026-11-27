"""Minimal stdlib .xlsx reader: cell values per sheet, plus which cell sides carry a red border.

Only what the indexers need: shared / inline strings and numbers (formulas give their cached value), and the
red frames Renesas pin lists draw around pins that must be used together. No formatting, no dates.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def col_index(ref: str) -> int:
    """'A1' -> 0, 'AE141' -> 30."""
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _row_index(ref: str) -> int:
    return int(re.search(r"\d+", ref).group(0))


class Sheet:
    def __init__(self, name: str, cells: dict[tuple[int, int], str], red: dict[tuple[int, int], set[str]]):
        self.name = name
        self.cells = cells        # (row 1-based, col 0-based) -> text
        self.red = red            # (row, col) -> {"left", "right", "top", "bottom"} drawn in red

    def get(self, row: int, col: int) -> str:
        return self.cells.get((row, col), "")

    def rows(self) -> list[int]:
        return sorted({r for r, _ in self.cells})


def _is_red(side: ET.Element | None) -> bool:
    if side is None or not side.get("style"):
        return False
    c = side.find(M + "color")
    return c is not None and (c.get("rgb", "").upper().endswith("FF0000") or c.get("indexed") == "10")


def read(path: Path) -> list[Sheet]:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(M + "si"):
                shared.append("".join(t.text or "" for t in si.iter(M + "t")))
        xf_red: list[set[str]] = []
        if "xl/styles.xml" in names:
            st = ET.fromstring(z.read("xl/styles.xml"))
            borders = [{s for s in ("left", "right", "top", "bottom") if _is_red(b.find(M + s))}
                       for b in st.find(M + "borders") or []]
            xf_red = [borders[int(x.get("borderId", 0))] if borders else set() for x in st.find(M + "cellXfs") or []]
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(PKG_R + "Relationship")}
        out = []
        for s in ET.fromstring(z.read("xl/workbook.xml")).find(M + "sheets"):
            target = rels[s.get(R + "id")].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            cells, red = {}, {}
            for c in ET.fromstring(z.read(target)).iter(M + "c"):
                key = (_row_index(c.get("r")), col_index(c.get("r")))
                style = int(c.get("s", 0))
                if style < len(xf_red) and xf_red[style]:
                    red[key] = xf_red[style]
                t, v = c.get("t"), c.find(M + "v")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(M + "t"))
                elif v is None or v.text is None:
                    continue
                elif t == "s":
                    val = shared[int(v.text)]
                else:
                    val = v.text
                cells[key] = val
            out.append(Sheet(s.get("name"), cells, red))
        return out


def drawing_text(path: Path) -> list[str]:
    """Text of every text box in the workbook's drawings (notices are often placed in text boxes)."""
    with zipfile.ZipFile(path) as z:
        out = []
        for n in sorted(x for x in z.namelist() if re.fullmatch(r"xl/drawings/drawing\d+\.xml", x)):
            d = z.read(n).decode("utf-8", "replace")
            for sp in re.findall(r"<xdr:txBody>(.*?)</xdr:txBody>", d, re.S):
                paras = [" ".join(re.findall(r"<a:t>([^<]*)</a:t>", p)) for p in re.findall(r"<a:p>(.*?)</a:p>", sp, re.S)]
                text = "\n".join(re.sub(r"\s+", " ", p).strip() for p in paras if p.strip())
                if text:
                    out.append(text)
        return out
