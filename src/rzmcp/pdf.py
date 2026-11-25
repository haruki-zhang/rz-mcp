"""Thin wrappers around poppler-utils (pdftotext / pdftohtml) plus page-text cleanup."""
from __future__ import annotations

import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def raw_page_texts(pdf: Path, first: int | None = None, last: int | None = None) -> list[str]:
    """Layout-preserving text, one string per page (index 0 == page `first` or 1)."""
    cmd = ["pdftotext", "-layout", "-enc", "UTF-8"]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    out = subprocess.run(cmd + [str(pdf), "-"], capture_output=True, check=True).stdout
    pages = out.decode("utf-8", "replace").split("\f")
    if pages and pages[-1].strip() == "":
        pages.pop()  # text after the final form feed
    return pages


def outline(pdf: Path) -> list[dict]:
    """PDF bookmarks in document order: [{level, title, page|None, group}] (level starts at 1).

    `group` is the title of the top-level ancestor, used to pick e.g. the schematic
    "Design Hierarchy" subtree.
    """
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftohtml", "-xml", "-i", "-q", "-enc", "UTF-8", "-f", "1", "-l", "1",
                        str(pdf), f"{d}/o"], check=True, capture_output=True)
        raw = (Path(d) / "o.xml").read_bytes().decode("utf-8", "replace")
    i, j = raw.find("<outline"), raw.rfind("</outline>")
    if i < 0 or j < 0:
        return []
    frag = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw[i:j + len("</outline>")])
    # Only the <outline> fragment is parsed: no DOCTYPE, so no entity expansion / XXE.
    if "<!DOCTYPE" in frag or "<!ENTITY" in frag:
        raise ValueError(f"unexpected DTD content in outline of {pdf}")
    root = ET.fromstring(frag)
    items: list[dict] = []

    def walk(node, level, group):
        last_title = group
        for child in node:
            if child.tag == "item":
                title = re.sub(r"\s+", " ", "".join(child.itertext())).strip()
                page = child.get("page")
                g = group if level > 1 else title
                items.append({"level": level, "title": title, "page": int(page) if page else None, "group": g})
                last_title = g
            elif child.tag == "outline":
                walk(child, level + 1, last_title)

    walk(root, 1, None)
    return items


def _first_segment_key(line: str) -> str:
    seg = re.split(r"\s{3,}", line.strip())[0]
    return re.sub(r"\d+", "#", seg)


def strip_running_lines(pages: list[str]) -> list[str]:
    """Remove running headers/footers: lines near the top/bottom of a page whose first
    column (digits normalised) recurs on at least half of the pages."""
    n = len(pages)
    if n < 4:
        return pages
    cands, cnt = [], Counter()
    for p in pages:
        lines = p.split("\n")
        idx = [i for i, l in enumerate(lines) if l.strip()]
        c = [(i, _first_segment_key(lines[i])) for i in dict.fromkeys(idx[:2] + idx[-3:])]
        cnt.update({k for _, k in c})
        cands.append(c)
    thr = max(3, n * 0.5)
    out = []
    for p, c in zip(pages, cands):
        # a dotted number ("#.#.#") is a section heading at the top of a page, never a running line
        drop = {i for i, k in c if cnt[k] >= thr and not re.fullmatch(r"#(\.#)+\.?", k)}
        out.append("\n".join(l for i, l in enumerate(p.split("\n")) if i not in drop))
    return out


def compact(text: str) -> str:
    """Shrink layout whitespace (~50% fewer chars) while keeping column separation."""
    text = re.sub(r"[ \t]{3,}", "   ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
