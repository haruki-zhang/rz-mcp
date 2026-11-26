"""Recognition test (plan 6.1): does an agent in an unrelated, empty directory pick the right rz-dev tool?

    .venv/bin/python eval/recognition.py [--repeat 5] [--jobs 4] [--model M] [--only id,id]

For every case in eval/recognition_cases.toml and every repetition, a fresh headless Claude Code session
(`claude -p`) runs in a new empty temporary directory, connected to nothing but a private rz-dev instance
started by this script. The first rz-dev tool the session invokes is recorded (positive cases stop right
there to save tokens). Pass: every positive case picks an expected tool in at least 4 of 5 runs
(scaled to --repeat); unrelated requests never invoke a build / setup / board-writing tool.
Results go to data/recognition/<time>.json.
"""
import argparse
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "rz-dev"
PREFIX = f"mcp__{NAME}__"
FORBID = {"yocto_setup", "poc_build", "poc_package", "poc_create", "board_flash", "board_exec", "board_verify",
          "job_cancel"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_one(case: dict, mcp_cfg: Path, model: str | None, timeout: float) -> dict:
    negative = not case["expect"]
    work = Path(tempfile.mkdtemp(prefix="rzrec-"))
    cmd = ["claude", "-p", case["prompt"], "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(mcp_cfg), "--strict-mcp-config", "--no-session-persistence",
           "--allowedTools", f"mcp__{NAME}", "--max-turns", "4" if negative else "8"]
    if model:
        cmd += ["--model", model]
    tools, first, t0, err = [], None, time.time(), None
    proc = subprocess.Popen(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if time.time() - t0 > timeout:
                err = "timeout"
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "assistant":
                continue
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    tools.append(c["name"])
                    if c["name"].startswith(PREFIX) and first is None:
                        first = c["name"][len(PREFIX):]
            if first and not negative:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        shutil.rmtree(work, ignore_errors=True)
    rz = [t[len(PREFIX):] for t in tools if t.startswith(PREFIX)]
    if negative:
        ok = not (set(rz) & FORBID)
    else:
        ok = first in case["expect"]
    return {"id": case["id"], "first": first, "tools": tools, "ok": ok, "seconds": round(time.time() - t0, 1),
            "error": err}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--timeout", type=float, default=240)
    a = ap.parse_args()
    cases = tomllib.load(open(ROOT / "eval" / "recognition_cases.toml", "rb"))["case"]
    if a.only:
        keep = set(a.only.split(","))
        cases = [c for c in cases if c["id"] in keep]

    port, tmp = free_port(), Path(tempfile.mkdtemp(prefix="rzrec-srv-"))
    token = (ROOT / "data" / ".token").read_text().strip()
    cfg = tmp / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {NAME: {"type": "http", "url": f"http://localhost:{port}/mcp",
                                                     "headers": {"Authorization": f"Bearer {token}"}}}}))
    srv = subprocess.Popen([sys.executable, "-m", "rzmcp.server", "--port", str(port), "--call-log",
                            str(tmp / "calls.jsonl")], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        runs = [c for c in cases for _ in range(a.repeat)]
        with ThreadPoolExecutor(a.jobs) as ex:
            results = list(ex.map(lambda c: run_one(c, cfg, a.model, a.timeout), runs))
    finally:
        srv.terminate()
        srv.wait(10)

    need = math.ceil(a.repeat * 0.8)
    all_ok = True
    print(f"{'case':<16} {'ok':>5}  first tools chosen")
    for c in cases:
        rs = [r for r in results if r["id"] == c["id"]]
        n_ok = sum(r["ok"] for r in rs)
        passed = n_ok == len(rs) if not c["expect"] else n_ok >= need
        all_ok &= passed
        firsts = ", ".join(sorted({f"{r['first'] or '-'}×{sum(x['first'] == r['first'] for x in rs)}" for r in rs}))
        print(f"{c['id']:<16} {n_ok}/{len(rs)}{'' if passed else ' FAIL'}  {firsts}"
              + ("  [errors: " + ", ".join(r["error"] for r in rs if r["error"]) + "]" if any(r["error"] for r in rs) else ""))
    out = ROOT / "data" / "recognition" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"repeat": a.repeat, "model": a.model, "passed": all_ok, "results": results},
                              ensure_ascii=False, indent=1))
    print(f"\n{'PASS' if all_ok else 'FAIL'} (positive cases need ≥{need}/{a.repeat}; unrelated requests must not "
          f"invoke {', '.join(sorted(FORBID))})\nresults: {out}")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
