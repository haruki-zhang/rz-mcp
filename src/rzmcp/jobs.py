"""Jobs: long-running work (Yocto setup and builds, flashing, verification) as detached processes.

A job lives in data/jobs/<id>/:
    job.json      what was asked: kind, params, title, planned steps, the physical operations it will need
    status.json   written only by the runner: state, step i/n, progress, human instruction, result
    log.txt       full log (runner stdout/stderr included)
    report.md     written by the job implementation when it has something to report
    cancel        flag file created by cancel()

The runner (`python -m rzmcp.jobs <dir>`) is independent of the MCP server: under systemd it gets its own
transient unit (systemd-run --user), otherwise its own session, so restarting the server never stops a job.
The server only reads status.json, plus a liveness check that turns a vanished runner into 'failed'.

Exclusive resources (the board's serial port) are flock()-ed by the runner itself; start() waits until the
runner has either taken the lock or reported it busy, so two concurrent starts cannot both get the board.

A job implementation is a function run(ctx) -> dict registered in KINDS. It calls ctx.step / ctx.log /
ctx.progress / ctx.wait_human, raises JobError(message, hint) on failure, and returns
{"summary": ..., "next": ...} (plus any extra result keys) on success.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from . import ROOT
from .config import JOBS_DIR

TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_WAIT = 50                      # seconds per job_status call; stays well below client HTTP timeouts
KINDS: dict[str, str] = {          # kind -> "module:function"
    "demo": "rzmcp.jobs:_demo",
    "poc_package": "rzmcp.poc:_package_job",
    "yocto_setup": "rzmcp.yocto:_setup_job",
    "poc_build": "rzmcp.yocto:_build_job",
}


class Busy(Exception):
    """An exclusive resource is held by another job (or by an interactive board_exec)."""

    def __init__(self, resource: str, owner: str):
        super().__init__(f"{resource} 正被 {owner} 占用")
        self.resource, self.owner = resource, owner


class JobError(Exception):
    """Expected failure of a job step: message says what went wrong, hint what to do next."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class Cancelled(BaseException):
    pass


# ---------------------------------------------------------------- files, locks, liveness

def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _proc_start(pid: int) -> str | None:
    """Start time of a live process (clock ticks since boot), to tell a live runner from a reused pid."""
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return None if f[0] in "ZX" else f[19]


def _alive(st: dict) -> bool:
    pid = st.get("pid")
    return bool(pid) and _proc_start(pid) == st.get("pid_start")


def _lock_paths(name: str, jobs_dir: Path) -> tuple[Path, Path]:
    d = jobs_dir / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.lock", d / f"{name}.owner"


def _try_lock(name: str, jobs_dir: Path) -> int | None:
    lock, _ = _lock_paths(name, jobs_dir)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def lock_owner(name: str, jobs_dir: Path = JOBS_DIR) -> str:
    _, owner = _lock_paths(name, jobs_dir)
    try:
        return owner.read_text().strip() or "未知的进程"
    except OSError:
        return "未知的进程"


@contextlib.contextmanager
def hold(name: str, owner: str, jobs_dir: Path = JOBS_DIR):
    """Hold an exclusive resource in this process (for instant tools such as board_exec); raises Busy."""
    fd = _try_lock(name, jobs_dir)
    if fd is None:
        raise Busy(name, lock_owner(name, jobs_dir))
    _, owner_file = _lock_paths(name, jobs_dir)
    owner_file.write_text(owner)
    try:
        yield
    finally:
        owner_file.write_text("")
        os.close(fd)


# ---------------------------------------------------------------- server side

def start(kind: str, params: dict, *, title: str, steps: list[str], human_ops: list[str] | None = None,
          poc: str | None = None, lock: str | None = None, jobs_dir: Path = JOBS_DIR) -> dict:
    """Create and launch a job; returns its status once the runner is up. Raises Busy if `lock` is taken."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    if lock:                                    # fast path: report a busy board without spawning anything
        fd = _try_lock(lock, jobs_dir)
        if fd is None:
            raise Busy(lock, lock_owner(lock, jobs_dir))
        os.close(fd)
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{secrets.token_hex(2)}"
    d = jobs_dir / job_id
    d.mkdir(parents=True)
    job = {"id": job_id, "kind": kind, "title": title, "params": params, "steps": steps,
           "human_ops": human_ops or [], "poc": poc, "lock": lock, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_json(d / "job.json", job)
    _write_json(d / "status.json", {"state": "starting", "step": 0, "of": len(steps), "updated": job["created"]})
    _spawn(d, job_id)
    deadline = time.time() + 15
    while time.time() < deadline:
        st = _read_json(d / "status.json")
        if st.get("state") != "starting":
            if st.get("busy"):
                raise Busy(lock, st["busy"])
            break
        time.sleep(0.1)
    return status(job_id, jobs_dir)


def _spawn(d: Path, job_id: str) -> None:
    pythonpath = os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")]))
    cmd = [sys.executable, "-m", "rzmcp.jobs", str(d)]
    if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        # running as a systemd service: give the job its own unit, so a service restart (which kills the
        # whole service cgroup) and the service's resource limits do not apply to it
        subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=rz-job-{job_id}",
                        f"--working-directory={ROOT}", f"--setenv=PYTHONPATH={pythonpath}", *cmd],
                       check=True, capture_output=True, timeout=30)
    else:
        # --detach: the runner forks once more and this child exits at once, so the runner is re-parented
        # to init and never becomes a zombie of the server
        subprocess.run([*cmd, "--detach"], cwd=ROOT, env={**os.environ, "PYTHONPATH": pythonpath},
                       start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)


def status(job_id: str, jobs_dir: Path = JOBS_DIR) -> dict:
    """job.json + status.json; a non-terminal job whose runner is gone is recorded as failed."""
    d = jobs_dir / job_id
    job = _read_json(d / "job.json")
    if not job:
        raise KeyError(job_id)
    st = _read_json(d / "status.json")
    if st.get("state") not in TERMINAL:
        age = time.time() - (d / "job.json").stat().st_mtime
        vanished = not _alive(st) and (st.get("pid") or age > 30)
        if vanished:                            # re-read: the runner may have finished just now
            st = _read_json(d / "status.json")
            vanished = st.get("state") not in TERMINAL
        if vanished:
            st.update(state="failed", updated=time.strftime("%Y-%m-%dT%H:%M:%S"),
                      message=f"作业进程意外退出（第 {st.get('step', 0)} 步）。日志：{d / 'log.txt'}",
                      hint="查看日志确认原因后，重新调用启动这个作业的工具；它会从当前状态接着做。")
            _write_json(d / "status.json", st)
    return {**job, **st, "dir": str(d)}


def wait(job_id: str, wait_seconds: float = 0, jobs_dir: Path = JOBS_DIR) -> dict:
    """Long-poll: return when state, step or human instruction changes, the job ends, or the wait expires."""
    st = status(job_id, jobs_dir)
    key = lambda s: (s.get("state"), s.get("step"), s.get("human"))
    k0, deadline = key(st), time.time() + max(0.0, min(wait_seconds, MAX_WAIT))
    while st.get("state") not in TERMINAL and time.time() < deadline:
        time.sleep(0.5)
        st = status(job_id, jobs_dir)
        if key(st) != k0:
            break
    return st


def cancel(job_id: str, jobs_dir: Path = JOBS_DIR) -> dict:
    st = status(job_id, jobs_dir)
    if st.get("state") in TERMINAL:
        return st
    d = jobs_dir / job_id
    (d / "cancel").touch()
    if _alive(st):
        os.kill(st["pid"], signal.SIGTERM)
        for _ in range(100):
            time.sleep(0.1)
            st = status(job_id, jobs_dir)
            if st.get("state") in TERMINAL:
                return st
        if _alive(st):
            os.kill(st["pid"], signal.SIGKILL)
    st = _read_json(d / "status.json")
    if st.get("state") not in TERMINAL:
        st.update(state="cancelled", message="作业已被强制结束", updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        _write_json(d / "status.json", st)
    return status(job_id, jobs_dir)


def list_jobs(limit: int = 10, jobs_dir: Path = JOBS_DIR) -> list[dict]:
    if not jobs_dir.is_dir():
        return []
    ids = sorted((p.name for p in jobs_dir.iterdir() if (p / "job.json").exists()), reverse=True)
    out = [status(i, jobs_dir) for i in ids]
    active = [s for s in out if s.get("state") not in TERMINAL]
    return active + [s for s in out if s.get("state") in TERMINAL][:limit]


# ---------------------------------------------------------------- agent-facing text

def _poll_line(job_id: str) -> str:
    return f"继续轮询：是。立即再次调用 job_status(job_id=\"{job_id}\", wait_seconds=50)，直到作业结束。"


def format_started(st: dict) -> str:
    lines = [f"已启动作业 {st['id']}：{st['title']}", "计划步骤：" + "；".join(
        f"{i}. {s}" for i, s in enumerate(st.get("steps", []), 1))]
    ops = st.get("human_ops") or []
    lines.append("整个作业需要用户做的物理操作：" + ("；".join(f"{i}. {o}" for i, o in enumerate(ops, 1))
                                          if ops else "无") + "。请先把这份清单告诉用户。")
    if st.get("state") in TERMINAL or st.get("state") == "waiting_human":
        lines.append(format_status(st))
    else:
        lines.append(_poll_line(st["id"]))
    return "\n".join(lines)


def format_status(st: dict) -> str:
    state, job_id = st.get("state"), st["id"]
    head = f"作业 {job_id}（{st.get('title', st.get('kind'))}）：{state}"
    if st.get("of"):
        head += f"，第 {st.get('step', 0)}/{st['of']} 步"
        if st.get("step_title"):
            head += f" {st['step_title']}"
    lines = [head]
    if st.get("progress"):
        lines.append(f"进度：{st['progress']}")
    if state == "waiting_human":
        lines += ["需要用户操作。把下面这句话原样告诉用户（用户不需要回复，工具会自己检测操作是否完成），"
                  "然后立即再次调用 job_status：", f">>> {st.get('human')}"]
    if st.get("recent"):
        lines.append("最近的日志：\n  " + "\n  ".join(st["recent"]))
    if state == "succeeded":
        lines.append(f"结果：{st.get('message', '完成')}")
        if st.get("next"):
            lines.append(f"下一步：{st['next']}")
    elif state in ("failed", "cancelled"):
        lines.append(f"{'失败' if state == 'failed' else '已取消'}：{st.get('message', '')}")
        if st.get("hint"):
            lines.append(f"建议：{st['hint']}")
    if state in TERMINAL:
        report = Path(st["dir"]) / "report.md"
        lines.append(f"作业目录：{st['dir']}" + (f"（报告 {report}）" if report.exists() else ""))
        lines.append("继续轮询：否，作业已结束。")
    else:
        lines.append(_poll_line(job_id))
    return "\n".join(lines)


# ---------------------------------------------------------------- runner side

class Ctx:
    """Handed to a job implementation; every change is written to status.json at once."""

    def __init__(self, d: Path, job: dict):
        self.dir, self.job, self.params = d, job, job["params"]
        self.st = _read_json(d / "status.json")
        self._log = open(d / "log.txt", "a", buffering=1, encoding="utf-8")

    def _set(self, **kw) -> None:
        self.st.update(kw, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        _write_json(self.dir / "status.json", self.st)

    def log(self, line: str, key: bool = False) -> None:
        """Append to log.txt; key lines are also shown in job_status (last 8)."""
        self._log.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        if key:
            self._set(recent=(self.st.get("recent", []) + [line])[-8:])

    def check_cancel(self) -> None:
        if (self.dir / "cancel").exists():
            raise Cancelled()

    def step(self, i: int, title: str | None = None) -> None:
        self.check_cancel()
        title = title or (self.job["steps"][i - 1] if 0 < i <= len(self.job["steps"]) else "")
        self.log(f"== step {i}: {title}", key=True)
        self._set(step=i, step_title=title, progress=None)

    def progress(self, text: str) -> None:
        self._set(progress=text)

    def wait_human(self, instruction: str, done: Callable[[], bool], timeout: float = 900, poll: float = 0.5,
                   timeout_hint: str = "") -> None:
        """Ask the user for a physical operation and wait until done() sees its effect (no keyboard reply)."""
        self.log(f"waiting for the user: {instruction}", key=True)
        self._set(state="waiting_human", human=instruction)
        deadline = time.time() + timeout
        while not done():
            self.check_cancel()
            if time.time() > deadline:
                raise JobError(f"等了 {int(timeout)} 秒，没有检测到用户完成操作：{instruction}",
                               timeout_hint or "确认操作已完成后，重新调用启动这个作业的工具；它会从当前状态接着做。")
            time.sleep(poll)
        self.log("user operation detected", key=True)
        self._set(state="running", human=None)

    def report(self, markdown: str) -> None:
        (self.dir / "report.md").write_text(markdown, encoding="utf-8")


def _run(d: Path) -> None:
    job = _read_json(d / "job.json")
    log = open(d / "log.txt", "a", buffering=1, encoding="utf-8")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    ctx = Ctx(d, job)

    def on_term(*_):
        (d / "cancel").touch()
        raise Cancelled()
    signal.signal(signal.SIGTERM, on_term)

    lock, fd, owner_file = job.get("lock"), None, None
    jobs_dir = d.parent
    try:
        if lock:
            fd = _try_lock(lock, jobs_dir)
            if fd is None:
                owner = lock_owner(lock, jobs_dir)
                ctx._set(state="failed", busy=owner, message=f"{lock} 正被 {owner} 占用",
                         hint="等那个作业结束，或用 job_cancel 取消它，然后重试。")
                return
            _, owner_file = _lock_paths(lock, jobs_dir)
            owner_file.write_text(f"作业 {job['id']}（{job['title']}）")
        ctx._set(state="running", pid=os.getpid(), pid_start=_proc_start(os.getpid()))
        mod, fn = KINDS[job["kind"]].split(":")
        result = getattr(importlib.import_module(mod), fn)(ctx) or {}
        ctx.log("job succeeded", key=True)
        ctx._set(state="succeeded", human=None, message=result.pop("summary", "完成"), next=result.pop("next", None),
                 result=result)
    except Cancelled:
        ctx.log("job cancelled")
        ctx._set(state="cancelled", human=None, message=f"在第 {ctx.st.get('step', 0)} 步被取消",
                 hint="重新调用启动这个作业的工具会从当前状态接着做。")
    except JobError as e:
        ctx.log(f"job failed: {e}", key=True)
        ctx._set(state="failed", human=None, message=str(e), hint=e.hint)
    except Exception as e:
        traceback.print_exc()
        ctx._set(state="failed", human=None, message=f"内部错误：{type(e).__name__}: {e}",
                 hint=f"这是 rz-mcp 的缺陷，完整的 traceback 在 {d / 'log.txt'}。")
    finally:
        if owner_file:
            owner_file.write_text("")
        if fd is not None:
            os.close(fd)


def _demo(ctx: Ctx) -> dict:
    """Test job: a timed step, a human operation detected by a file appearing, a final step."""
    p = ctx.params
    ctx.step(1)
    time.sleep(p.get("sleep", 0.5))
    if p.get("fail"):
        raise JobError("演示作业按要求失败", "这是测试用的失败。")
    ctx.step(2)
    ctx.wait_human(p.get("instruction", "请创建文件 " + p["wait_file"]), lambda: Path(p["wait_file"]).exists(),
                   timeout=p.get("timeout", 120))
    ctx.step(3)
    time.sleep(p.get("sleep", 0.5))
    ctx.report("# 演示作业\n\n完成。\n")
    return {"summary": "演示作业完成", "next": "无"}


if __name__ == "__main__":
    if "--detach" in sys.argv and os.fork() > 0:
        os._exit(0)
    from rzmcp import jobs as _jobs        # run the importable module, so JobError / Cancelled are its classes
    _jobs._run(Path(sys.argv[1]))
