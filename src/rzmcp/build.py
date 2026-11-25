"""Rebuild the database safely: build into data/rz.db.tmp, sanity-check it, then atomically swap it in.

    .venv/bin/python -m rzmcp.build                  # layer A + B from scratch (~30 s)
    .venv/bin/python -m rzmcp.build --registers      # keep layer A, rebuild only layer B (seconds)
    .venv/bin/python -m rzmcp.build --restart        # afterwards: systemctl --user try-restart rz-mcp

A running server keeps reading the old file (its open descriptors point to the old inode) until it is
restarted, so it never sees a half-built database. If the sanity check fails the current database is
left untouched. Prefer this over running build_fulltext / build_registers directly on data/rz.db.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import DB_PATH, build_fulltext, build_registers, catalog

SERVICE = "rz-mcp"


def sanity(db: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a freshly built database."""
    m = catalog.load()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    one = lambda sql: con.execute(sql).fetchone()[0]
    errors, warnings = [], []
    if one("PRAGMA integrity_check") != "ok":
        errors.append("PRAGMA integrity_check failed")
    if (n := one("SELECT COUNT(*) FROM document")) != len(m["documents"]):
        errors.append(f"document rows {n} != manifest documents {len(m['documents'])}")
    want_pdf = sum(1 for d in m["documents"] if "fulltext" in d.get("index", []) and d["path"].lower().endswith(".pdf"))
    if (n := one("SELECT COUNT(DISTINCT doc_id) FROM page")) != want_pdf:
        errors.append(f"text-indexed documents {n} != expected {want_pdf}")
    if one("SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH 'register'") == 0:
        errors.append("full-text index returns nothing for 'register'")
    want_mod = sum(len(d.get("register_modules", [])) for d in m["documents"])
    if (n := one("SELECT COUNT(*) FROM module")) != want_mod:
        errors.append(f"register modules {n} != manifest register_modules {want_mod}")
    for (name,) in con.execute("SELECT name FROM module m WHERE NOT EXISTS "
                               "(SELECT 1 FROM register r WHERE r.module_id = m.module_id)"):
        warnings.append(f"module {name}: no registers parsed from its register list")
    for name, n in con.execute("SELECT m.name, COUNT(*) FROM register_issue i JOIN module m USING(module_id) "
                               "WHERE i.rule NOT LIKE 'info_%' GROUP BY m.name"):
        warnings.append(f"module {name}: {n} extraction-check issue(s) (see register_issue table)")
    con.close()
    return errors, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--registers", action="store_true", help="reuse layer A from the current database")
    ap.add_argument("--restart", action="store_true", help=f"systemctl --user try-restart {SERVICE} afterwards")
    a = ap.parse_args()
    db: Path = a.db
    tmp = db.with_name(db.name + ".tmp")
    t0 = time.time()
    tmp.unlink(missing_ok=True)
    try:
        if a.registers:
            if not db.exists():
                sys.exit(f"{db} does not exist; run a full build first (without --registers)")
            src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp)
            src.backup(dst)
            src.close()
            dst.close()
        else:
            build_fulltext.build(tmp)
        build_registers.build(tmp)
        errors, warnings = sanity(tmp)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        if errors:
            sys.exit("sanity check FAILED — current database left unchanged:\n  " + "\n  ".join(errors))
        os.replace(tmp, db)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"database updated: {db} ({db.stat().st_size / 1e6:.0f} MB, {time.time() - t0:.0f}s)", file=sys.stderr)

    active = subprocess.run(["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True).stdout.strip()
    if a.restart:
        subprocess.run(["systemctl", "--user", "try-restart", SERVICE], check=False)
        print(f"{SERVICE}: try-restart issued (was {active or 'unknown'})", file=sys.stderr)
    elif active == "active":
        print(f"note: {SERVICE} is running and still serves the previous data — "
              f"run: systemctl --user restart {SERVICE}", file=sys.stderr)


if __name__ == "__main__":
    main()
