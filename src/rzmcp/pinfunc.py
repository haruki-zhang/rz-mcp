"""Pin function list (.xlsx, sheet "Pin Function") -> one text record per pin, plus one record of notes.

Layout of the Renesas RZ/G3L / RZ/G3SE pin function list (LLWEB-20218308, Rev 1.1), found by header text so
that a revision that moves columns fails loudly instead of indexing the wrong cells:
  row "Pin Name"     : ball location (one column per package), pin name, classification, power domain,
                       voltage, IO type, #GPIO, dedicated pin (classification / function / IO),
                       "# Function0" .. "# Function15" (classification / pin function / IO each),
                       pin conditions at SYS_RST#=L and per boot mode (pin name / state each),
                       "Handling when the Pin is not in Use", PU/PD, Schmitt
  row above it       : "# Port mode (PMC=0)" (GPIO classification / function / IO)
Red frames around function cells mark pins that must be used together (RSCI, RSPI, RIIC, SSI, PDM, SPDIF).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import xlsx

SHEET = "Pin Function"


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s.replace("㎜", "mm").replace("□", "")).strip()
    return "" if s in ("-", "") else s


def _find(sheet: xlsx.Sheet, row: int, pattern: str) -> list[tuple[int, re.Match]]:
    out = []
    for (r, c), v in sheet.cells.items():
        if r == row and (m := re.match(pattern, v.strip(), re.S)):
            out.append((c, m))
    return sorted(out, key=lambda x: x[0])


def _layout(sheet: xlsx.Sheet) -> dict:
    hdr = next((r for (r, c), v in sheet.cells.items() if v.strip() == "Pin Name" and c < 10), None)
    if hdr is None:
        raise ValueError(f"sheet {sheet.name!r}: no 'Pin Name' header")
    sub = hdr + 1
    name_col = next(c for (r, c), v in sheet.cells.items() if r == hdr and v.strip() == "Pin Name")
    funcs = {int(m.group(1)): c for c, m in _find(sheet, hdr, r"# Function(\d+)$")}
    port = [c for c, _ in _find(sheet, hdr - 1, r"# Port mode")]
    rst_l = [c for c, _ in _find(sheet, hdr - 1, r"Pin condition at\s+SYS_RST#=L")]
    boots = [(c, m.group(1), _clean(m.group(2))) for c, m in
             _find(sheet, hdr, r"Pin condition\s+after power-on reset deas+ert MD_BOOT2-0\s*=\s*(\d+b)\s*\((.*?)\)")]
    unused = [c for c, _ in _find(sheet, hdr, r"Handling when the Pin is not in Use")]
    pupd = [c for c, _ in _find(sheet, sub, r"PU/PD")]
    schmitt = [c for c, _ in _find(sheet, sub, r"Schmitt")]
    balls = [(c, _clean(sheet.get(sub, c))) for c in range(name_col) if _clean(sheet.get(sub, c))]
    if sorted(funcs) != list(range(16)) or not port or not rst_l or not unused or not balls:
        raise ValueError(f"sheet {sheet.name!r}: unexpected layout (functions {sorted(funcs)}, port {port}, "
                         f"reset {rst_l}, unused {unused}, balls {balls})")
    return {"hdr": hdr, "first": sub + 1, "name": name_col, "balls": balls, "funcs": funcs, "port": port[0],
            "rst_l": rst_l[0], "boots": boots, "unused": unused[0], "pupd": pupd[0] if pupd else None,
            "schmitt": schmitt[0] if schmitt else None}


def _frames(sheet: xlsx.Sheet, c0: int, rows: list[int]) -> list[list[int]]:
    """Runs of rows enclosed by a red frame over the three cells (classification, function, IO) at c0.

    A frame edge may be stored on either cell that shares it (top of this row or bottom of the row above;
    left of c0 or right of c0-1). The middle column belongs to this block alone, so a frame starts at a top
    edge and ends at a bottom edge of the middle cell; the side edges only confirm that a row is enclosed.
    """
    red = lambda r, c: sheet.red.get((r, c), set())
    mid = c0 + 1
    top = lambda r: "top" in red(r, mid) or "bottom" in red(r - 1, mid)
    bottom = lambda r: "bottom" in red(r, mid) or "top" in red(r + 1, mid)
    side = lambda r: ("left" in red(r, c0) or "right" in red(r, c0 - 1)
                      or "right" in red(r, c0 + 2) or "left" in red(r, c0 + 3))
    groups, cur = [], None
    for r in rows:
        if cur is not None and not side(r):
            cur = None                       # not enclosed after all: drop the open run
        if cur is None and top(r) and side(r):
            cur = []
        if cur is not None:
            cur.append(r)
            if bottom(r):
                # gaps between frames look like frames too (shared edges); a real frame encloses functions only
                if all(_clean(sheet.get(x, mid)) for x in cur):
                    groups.append(cur)
                cur = None
    return groups


def records(path: Path) -> list[dict]:
    """[{"title", "locator", "body"}]: the notes record first, then one record per pin row."""
    sheets = {s.name: s for s in xlsx.read(path)}
    sh = sheets[SHEET]
    L = _layout(sh)
    rows = [r for r in sh.rows() if r >= L["first"] and _clean(sh.get(r, L["name"]))]
    pin = {r: _clean(sh.get(r, L["name"])).split(" (")[0] for r in rows}

    partners: dict[tuple[int, int], list[int]] = {}
    for f, c0 in [(-1, L["port"])] + sorted(L["funcs"].items()):
        for g in _frames(sh, c0, rows):
            if len(g) > 1:
                for r in g:
                    partners[(r, f)] = [x for x in g if x != r]

    def fn(r: int, c0: int) -> str:
        cls, name, io = (_clean(sh.get(r, c)) for c in (c0, c0 + 1, c0 + 2))
        # the signal name already carries module and channel (SCIF Ch1 / SCIF1_RXD): keep only the classification
        # words it lacks, so that module words like SCIF / Ch5 are not repeated in 400 records (their weight in
        # the manuals' ranking would drop)
        cls = " ".join(w for w in re.findall(r"[A-Za-z0-9]+", re.sub(r"\bCh\s*\d+\b", "", cls, flags=re.I))
                       if w.upper() not in name.upper())
        return " ".join(x for x in (f"{cls}:" if cls else "", name, f"({io})" if io else "") if x)

    out = [_notes(sheets, sh, L, path)]
    for r in rows:
        raw = _clean(sh.get(r, L["name"]))
        name = pin[r]
        balls = ", ".join(f"{_clean(sh.get(r, c))} ({pkg} package)" for c, pkg in L["balls"] if _clean(sh.get(r, c)))
        kind = _clean(sh.get(r, L["name"] + 1))
        lines = [f"Pin {raw}: {kind or 'pin'}; ball {balls or '-'}"]
        dom, volt, iot = (_clean(sh.get(r, L["name"] + i)) for i in (2, 3, 4))
        if dom or volt or iot:
            lines.append("Power domain " + ", ".join(x for x in (dom, f"{volt} V" if volt else "", f"IO type: {iot}" if iot else "") if x))
        gpio = _clean(sh.get(r, L["name"] + 5))
        ded = fn(r, L["name"] + 6)
        if ded:
            lines.append(f"Dedicated pin: {ded}")
        if gpio or fn(r, L["port"]):
            lines.append(f"GPIO port {gpio or '-'}; port mode (PMC=0): {fn(r, L['port']) or '-'}")
        mux = []
        for f, c0 in sorted(L["funcs"].items()):
            if text := fn(r, c0):
                p = partners.get((r, f))
                mux.append(f"  Function {f}: {text}" + (
                    "  [red frame group: " + ", ".join(f"{pin[x]} {_clean(sh.get(x, c0 + 1))}" for x in p) + "]"
                    if p else ""))
        if mux:
            lines.append("Multiplexed functions (PMC=1, PFC Function n):")
            lines += mux
        rst = " ".join(x for x in (_clean(sh.get(r, L["rst_l"])), _clean(sh.get(r, L["rst_l"] + 1))) if x)
        if rst:
            lines.append(f"Pin condition at SYS_RST#=L: {rst}")
        boot = [f"{code} {label}: " + " ".join(x for x in (_clean(sh.get(r, c)), _clean(sh.get(r, c + 1))) if x)
                for c, code, label in L["boots"] if _clean(sh.get(r, c)) or _clean(sh.get(r, c + 1))]
        if boot:
            lines.append("After power-on reset by MD_BOOT2-0 = " + "; ".join(boot))
        tail = [f"if unused: {_clean(sh.get(r, L['unused']))}" if _clean(sh.get(r, L["unused"])) else ""]
        if L["pupd"] is not None and _clean(sh.get(r, L["pupd"])):
            tail.append(f"PU/PD: {_clean(sh.get(r, L['pupd']))}")
        if L["schmitt"] is not None and _clean(sh.get(r, L["schmitt"])):
            tail.append(f"Schmitt: {_clean(sh.get(r, L['schmitt']))}")
        if any(tail):
            lines.append("; ".join(t for t in tail if t))
        out.append({"title": f"{name} pin", "locator": f"pin {name}", "body": "\n".join(lines)})
    return out


def _notes(sheets: dict, sh: xlsx.Sheet, L: dict, path: Path) -> dict:
    """Symbols, numbered notes (*n), text-box notices and revision history: one record."""
    lines = []
    for r in sorted({r for (r, c) in sh.cells if r < L["hdr"] - 1}):
        for c in sorted(c for (rr, c) in sh.cells if rr == r):
            if t := _clean(sh.get(r, c)):
                lines.append(t)
    side = [_clean(v) for (r, c), v in sorted(sh.cells.items()) if r >= L["hdr"] and c > L["schmitt"] + 1 and _clean(v)] \
        if L["schmitt"] is not None else []
    lines += side
    lines += [t.replace("\n", " ") for t in xlsx.drawing_text(path)]
    hist = next((s for n, s in sheets.items() if n.lower() == "history"), None)
    if hist:
        lines.append("Revision history:")
        for r in hist.rows():
            row = [_clean(hist.get(r, c)) for c in sorted(c for (rr, c) in hist.cells if rr == r)]
            row = [re.sub(r"^(\d+\.\d)0{6,}\d*$", r"\1", x) for x in row if x]   # 1.1000000000000001 -> 1.1
            if row:
                lines.append("  " + " | ".join(row))
    return {"title": "Pin function list notes, symbols and pin combination restrictions", "locator": "notes",
            "body": "\n".join(lines)}
