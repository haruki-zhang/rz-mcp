"""Job framework tests (no hardware): a demo job goes through waiting_human, survives a server restart and is
still answered by job_status; failure, cancel, the exclusive lock, a killed runner and the systemd-run path.

    .venv/bin/python eval/test_jobs.py

Uses the real data/jobs directory (the server reads it) and removes the jobs it created. Starts its own
server instance on a spare port.
"""
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rzmcp import jobs  # noqa: E402

PY = sys.executable
created: list[str] = []
tmp = Path(tempfile.mkdtemp(prefix="rzjobs-"))


def demo(**params) -> dict:
    params.setdefault("wait_file", str(tmp / f"op-{len(created)}"))
    st = jobs.start("demo", params, title="演示作业", steps=["准备", "等待人操作", "收尾"],
                    human_ops=["创建一个文件"], lock=params.pop("lock", None))
    created.append(st["id"])
    return st


def until(job_id: str, pred, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = jobs.status(job_id)
        if pred(st):
            return st
        time.sleep(0.2)
    raise AssertionError(f"timeout; last status {jobs.status(job_id)}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, port: int):
        self.port, self.proc = port, None

    def start(self):
        self.proc = subprocess.Popen([PY, "-m", "rzmcp.server", "--port", str(self.port),
                                      "--call-log", str(tmp / "calls.jsonl")], cwd=ROOT,
                                     env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(10)

    def call(self, tool: str, **args) -> str:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        token = (ROOT / "data" / ".token").read_text().strip()

        async def go():
            async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as hc:
                async with streamable_http_client(f"http://localhost:{self.port}/mcp", http_client=hc) as streams:
                    async with ClientSession(streams[0], streams[1]) as s:
                        await s.initialize()
                        res = await s.call_tool(tool, args)
                        return res.content[0].text
        return asyncio.run(go())


def test_happy_path_and_restart():
    srv = Server(free_port())
    srv.start()
    st = demo(sleep=0.3)
    text = jobs.format_started(st)
    assert "创建一个文件" in text and "job_status" in text, text
    out = srv.call("job_status", job_id=st["id"], wait_seconds=20)
    assert "waiting_human" in out and ">>> 请创建文件" in out and "原样告诉用户" in out, out
    srv.stop()                                         # the job must not care
    assert jobs.status(st["id"])["state"] == "waiting_human"
    srv.start()
    out = srv.call("job_status", job_id=st["id"])
    assert "waiting_human" in out, out
    Path(st["params"]["wait_file"]).touch()
    out = srv.call("job_status", job_id=st["id"], wait_seconds=20)
    if "succeeded" not in out:                         # woke up on the step change; poll once more
        out = srv.call("job_status", job_id=st["id"], wait_seconds=20)
    assert "succeeded" in out and "继续轮询：否" in out and "report.md" in out, out
    listing = srv.call("job_status")
    assert st["id"] in listing, listing
    # cancel through MCP
    st2 = demo()
    until(st2["id"], lambda s: s["state"] == "waiting_human")
    out = srv.call("job_cancel", job_id=st2["id"])
    assert "cancelled" in out and "第 2 步" in out, out
    assert "没有作业" in srv.call("job_status", job_id="nope")
    srv.stop()


def test_failure():
    st = demo(fail=True)
    st = until(st["id"], lambda s: s["state"] in jobs.TERMINAL)
    assert st["state"] == "failed" and st["message"] == "演示作业按要求失败" and st["hint"], st
    assert "建议：" in jobs.format_status(st)


def test_lock():
    a = demo(lock="testboard")
    until(a["id"], lambda s: s["state"] == "waiting_human")
    try:
        demo(lock="testboard")
        raise AssertionError("second job got the lock")
    except jobs.Busy as e:
        assert a["id"] in e.owner, e.owner
    try:
        with jobs.hold("testboard", "board_exec"):
            raise AssertionError("hold() got the lock")
    except jobs.Busy:
        pass
    jobs.cancel(a["id"])
    with jobs.hold("testboard", "board_exec"):        # released when the runner ended
        assert jobs.lock_owner("testboard") == "board_exec"
    b = demo(lock="testboard")
    until(b["id"], lambda s: s["state"] == "waiting_human")
    jobs.cancel(b["id"])


def test_killed_runner():
    st = demo()
    st = until(st["id"], lambda s: s["state"] == "waiting_human")
    os.kill(st["pid"], signal.SIGKILL)
    st = until(st["id"], lambda s: s["state"] in jobs.TERMINAL)
    assert st["state"] == "failed" and "意外退出" in st["message"], st


def test_systemd_run():
    if not shutil.which("systemd-run") or subprocess.run(["systemctl", "--user", "is-system-running"],
                                                         capture_output=True).returncode not in (0, 1):
        print("  (skipped: no systemd user manager)")
        return
    os.environ["INVOCATION_ID"] = "test"
    try:
        st = demo()
    finally:
        del os.environ["INVOCATION_ID"]
    until(st["id"], lambda s: s["state"] == "waiting_human")
    unit = f"rz-job-{st['id']}.service"
    r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
    assert r.stdout.strip() == "active", r
    Path(st["params"]["wait_file"]).touch()
    st = until(st["id"], lambda s: s["state"] in jobs.TERMINAL)
    assert st["state"] == "succeeded", st


def main():
    tests = [test_failure, test_lock, test_killed_runner, test_systemd_run, test_happy_path_and_restart]
    failed = 0
    try:
        for t in tests:
            try:
                t()
                print(f"ok   {t.__name__}")
            except Exception as e:
                failed += 1
                print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    finally:
        for i in created:
            try:
                jobs.cancel(i)
            except Exception:
                pass
            shutil.rmtree(jobs.JOBS_DIR / i, ignore_errors=True)
        for name in ("testboard.lock", "testboard.owner"):
            (jobs.JOBS_DIR / "locks" / name).unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
