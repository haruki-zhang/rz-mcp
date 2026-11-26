# Run: /usr/bin/python3 eval/test_watch.py      (system python3: needs pyserial; writes only under /tmp/watch-tests)
"""Known-answer tests for running board jobs in the background (P1, 2026-11-26): evk-watch follows the job that
holds the console and prints one line per event, so the agent can pass ">>> 需要你操作" on to the user.

The simulated operator reacts to evk-watch's output, not to the job's own output: that is the real loop (the
agent's Monitor sees the line, tells the user, the user powers the board). Jobs: a PoC-style script that uses only
evk.Console (no progress of its own, like the evk-mac Step 0 scripts), evk-flash, evk-verify."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
BOARD = RZ / "workflow" / "board"
sys.path.insert(0, str(RZ / "eval"))
sys.path.insert(0, str(BOARD))
from fake_uboot import FakeUBoot  # noqa: E402
import test_board as TB           # noqa: E402  (synthetic images, the deploy directory)

W = Path("/tmp/watch-tests")
ACTIVE = W / "active.json"
ENV = {**os.environ, "RZ_BOARD_ACTIVE": str(ACTIVE), "PYTHONUNBUFFERED": "1"}
WATCH = BOARD / "evk-watch"
fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    if not c:
        fails.append(m)


JOB = '''
import sys, time
sys.path.insert(0, {board!r})
import evk
c = evk.Console({port!r}, log={log!r})
c.stop_autoboot(30, "给 EVK 上电（eMMC 启动），工具会自动打断 U-Boot。")
c.cmd("printenv")
mode = sys.argv[1]
if mode == "result":
    c.result(True, "S0 done")
elif mode == "raise":
    raise evk.BoardError("OTP read failed")
elif mode == "hang":
    time.sleep(120)
'''


def watched(job_cmd: list, fake: FakeUBoot | None, watch_args=(), env=ENV, on_line=None, timeout=600):
    """Start evk-watch, then the job; an operator thread powers the fake board when evk-watch asks for a person."""
    w = subprocess.Popen([sys.executable, "-u", str(WATCH), "--wait", "30", *watch_args], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=env)
    lines = []

    def operator():
        for ln in w.stdout:
            lines.append(ln.rstrip("\n"))
            if fake and ln.startswith(">>> 需要你操作"):
                mode = "scif" if "SCIF" in ln else "qspi" if "QSPI 启动" in ln else "emmc"
                threading.Timer(0.5, fake.power_on, args=(mode,)).start()
            if on_line:
                on_line(ln)

    th = threading.Thread(target=operator, daemon=True)
    th.start()
    time.sleep(0.5)
    job = subprocess.Popen(job_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    rc = w.wait(timeout=timeout)
    th.join(5)
    try:
        jout = job.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        job.kill()
        jout = job.communicate()[0]
    if fake:
        fake.stop = True
    return rc, lines, job, jout


def script(mode: str, fake: FakeUBoot, name: str) -> list:
    f = W / f"{name}.py"
    f.write_text(JOB.format(board=str(BOARD), port=fake.port, log=str(W / f"{name}.log")))
    return [sys.executable, "-u", str(f), mode]


subprocess.run(["rm", "-rf", str(W)])
W.mkdir(parents=True)

# ---------------------------------------------------------------- a PoC's own script: evk.Console only
f = FakeUBoot(W, W / "sd1.img", 1 << 20)
rc, lines, job, out = watched(script("none", f, "s1"), f)
text = "\n".join(lines)
check(any(l.startswith(">>> 需要你操作：给 EVK 上电") for l in lines), "PoC script without a progress of its own: evk-watch passes on its ask_human")
check("✓ 检测到板子有输出" in text, "the board's output marks the person's action done")
check(rc == 0 and lines[-1].startswith("结束：进程已退出，没有报告结果") and job.returncode == 0,
      f"no result reported: closed at exit, evk-watch exits 0 ({lines[-1:]})")
check((W / "s1.status.json").exists() and (W / "s1.status.events.jsonl").exists(), "status and events are written next to the script's log")
check(not ACTIVE.exists(), "the job is unregistered when it ends")
check(job.returncode == 0 and "需要你操作" in out, "the script's own output still shows the prompt (for a person at a terminal)")

f = FakeUBoot(W, W / "sd2.img", 1 << 20)
rc, lines, job, _ = watched(script("result", f, "s2"), f)
check(rc == 0 and lines[-1].startswith("完成：S0 done"), f"Console.result(True, ...) -> '完成：…', exit 0 ({lines[-1:]})")

f = FakeUBoot(W, W / "sd3.img", 1 << 20)
rc, lines, job, _ = watched(script("raise", f, "s3"), f)
check(rc == 1 and lines[-1].startswith("失败：脚本异常退出：BoardError: OTP read failed") and job.returncode == 1,
      f"uncaught exception -> '失败：…', exit 1 ({lines[-1:]})")

f = FakeUBoot(W, W / "sd4.img", 1 << 20)


def kill_when_ready(ln):                               # kill -9: no exit handler runs, the record stays behind
    if "✓ 检测到板子有输出" in ln:
        pid = json.loads(ACTIVE.read_text())["pid"]
        threading.Timer(1.5, lambda: os.kill(pid, signal.SIGKILL)).start()


rc, lines, job, _ = watched(script("hang", f, "s4"), f, on_line=kill_when_ready, timeout=120)
check(rc == 2 and "进程已经不在了" in lines[-1], f"job killed (no exit handler) -> evk-watch says so, exit 2 ({lines[-1:]})")
ACTIVE.unlink(missing_ok=True)

# ---------------------------------------------------------------- no job / a stale record / --status
r = subprocess.run([sys.executable, str(WATCH), "--wait", "1"], capture_output=True, text=True, env=ENV)
check(r.returncode == 2 and "没有正在运行的上板任务" in r.stdout, "no job within --wait -> exit 2")
ACTIVE.write_text(json.dumps({"status": str(W / "x.status.json"), "events": str(W / "x.events.jsonl"), "pid": 999999, "command": "old"}))
r = subprocess.run([sys.executable, str(WATCH), "--wait", "1"], capture_output=True, text=True, env=ENV)
check(r.returncode == 2 and "没有正在运行" in r.stdout, "a record whose process is gone is ignored")
ACTIVE.unlink()

# ---------------------------------------------------------------- the real console only (no registry pollution by tests)
real = RZ / "data" / "board-runs" / "active.json"
before = real.read_text() if real.exists() else None
f = FakeUBoot(W, W / "sd5.img", 1 << 20)
env = {k: v for k, v in ENV.items() if k != "RZ_BOARD_ACTIVE"}
p = subprocess.Popen(script("none", f, "s5"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
for ln in p.stdout:
    if "需要你操作" in ln:
        threading.Timer(0.5, f.power_on, args=("emmc",)).start()
p.wait(60)
f.stop = True
check((real.read_text() if real.exists() else None) == before, "a pty (fake board) is not registered in the real data/board-runs/active.json")

# ---------------------------------------------------------------- evk-flash through evk-watch
fip, cm33 = (TB.DEPLOY / "fip-smarc-rzg3l.bin").read_bytes(), (TB.DEPLOY / f"{TB.CM33}.bin").read_bytes()
img = TB.synthetic_image(W / "syn.rootfs.wic.gz", fip, cm33)
f = FakeUBoot(W / "tftp", W / "emmc.img", boot1=W / "boot1.img", qspi=W / "qspi.img")
(W / "tftp").mkdir(exist_ok=True)
cmd = [sys.executable, "-u", str(BOARD / "evk-flash"), "emmc", "--image", str(img), "--deploy", str(TB.DEPLOY),
       "--tftp-root", str(W / "tftp"), "--log-dir", str(W / "logs"), "--wait", "30", "--skip-host-checks", "--port", f.port]
rc, lines, job, out = watched(cmd, f, timeout=1200)
asks = [l for l in lines if l.startswith(">>> 需要你操作")]
check(len(asks) == 2 and "SCIF" in asks[0] and "eMMC" in asks[1], f"evk-flash emmc: evk-watch passes on both manual steps ({[a[:40] for a in asks]})")
check(rc == 0 and job.returncode == 0 and lines[-1].startswith("完成：emmc 完成"), f"evk-flash emmc: the end is reported, exit 0 ({lines[-1:]})")
check(sum(l.startswith("[") for l in lines) >= 5 and any("✓" in l for l in lines), "steps and their results are shown")

# ---------------------------------------------------------------- evk-verify through evk-watch --quiet
poc = W / "rzg3l-w"
(poc / "design").mkdir(parents=True)
(poc / "design" / "verify.toml").write_text('[[check]]\nid = "V-1"\ntitle = "kernel"\nwhere = "linux"\nrun = "uname -r"\nexpect = "^6\\\\.12"\n'
                                            '[[check]]\nid = "V-2"\ntitle = "quiet"\nwhere = "linux"\nrun = "cat /proc/cmdline"\nexpect = "quiet"\n')
f = FakeUBoot(W, W / "sd6.img", 1 << 20, state="login",
              linux_cmds={"uname -r": ("6.12.46-cip8", 0), "cat /proc/cmdline": ("rw root=/dev/mmcblk0p2", 0)})
rc, lines, job, _ = watched([sys.executable, "-u", str(BOARD / "evk-verify"), str(poc), "--port", f.port, "--wait", "20"],
                            f, watch_args=("--quiet",))
check(rc == 1 and len(lines) == 3 and "✗ V-2 quiet" in lines[1] and lines[2].startswith("失败：1/2 passed"),
      f"evk-verify, --quiet: only the failed check and the end ({lines})")
r = subprocess.run([sys.executable, str(WATCH), "--status"], capture_output=True, text=True, env=ENV)
check(r.returncode == 0 and "状态：" in r.stdout, "--status prints the state of the latest job")

subprocess.run(["rm", "-rf", str(W)])
print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
