# Run: /usr/bin/python3 eval/test_progress.py   (system python3: needs pyserial)
"""Tests for workflow/board/progress.py and the progress display of evk-flash (terminal and non-terminal)."""
import io
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import threading
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RZ / "eval"))
sys.path.insert(0, str(RZ / "workflow" / "board"))
import progress as P  # noqa: E402
from fake_uboot import FakeUBoot  # noqa: E402
from test_board import synthetic_image, DEPLOY, CM33  # noqa: E402

W = Path("/tmp/progress-tests")
TOOL = RZ / "workflow" / "board" / "evk-flash"
fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    if not c:
        fails.append(m)


shutil.rmtree(W, ignore_errors=True)
W.mkdir()

# ---- unit: non-terminal output and status.json
buf = io.StringIO()
st = W / "status.json"
pg = P.Progress(st, tty=False, stream=buf)
pg.set_plan([("a", "第一步", 5), ("b", "第二步", 60)])
pg.start("a")
pg.finish("ok")
pg.start("b", "detail")
for i in range(0, 101):
    pg.update(i, 100)
s = json.loads(st.read_text())
out = buf.getvalue()
check("\r" not in out and "[1/2] 第一步" in out and "[2/2] 第二步" in out, "non-terminal: step headers, no carriage returns")
check(8 <= out.count("进度 ") <= 12, f"non-terminal: a progress line every 10 % ({out.count('进度 ')} lines)")
check(s["step"] == 2 and s["steps"] == 2 and s["done"] == 100 and s["total"] == 100, "status.json: step and progress")
pg.human("拨开关")
s = json.loads(st.read_text())
check(s["state"] == "waiting_for_human" and s["human"] == "拨开关" and "需要你操作：拨开关" in buf.getvalue(), "human request: shown and in status.json")
check("等你操作" in P.describe(s) and "需要你操作：拨开关" in P.describe(s), "describe(): the state a person must act on")
pg.human_done()
check(json.loads(st.read_text())["state"] == "running" and "✓ 检测到板子有输出" in buf.getvalue(), "board output ends the wait, with feedback")
pg.end(True, "done")
check(json.loads(st.read_text())["state"] == "done", "end(): state done")

# ---- unit: terminal output redraws one line
buf = io.StringIO()
pg = P.Progress(None, tty=True, stream=buf)
pg.set_plan([("a", "写入", 10)])
pg.start("a")
pg.update(50, 100)
pg.update(100, 100)
check("\r\033[K" in buf.getvalue() and "█" in buf.getvalue() and "100%" in buf.getvalue(), "terminal: bar redrawn in place")
check("\033[" in buf.getvalue(), "terminal: colours")
buf = io.StringIO()
os.environ["NO_COLOR"] = "1"
pg = P.Progress(None, tty=True, stream=buf)
pg.start("x")
check("\033[1m" not in buf.getvalue(), "NO_COLOR turns colours off")
del os.environ["NO_COLOR"]

# ---- evk-flash rootfs on a pty (a real terminal for the tool), board at the U-Boot prompt
img = synthetic_image(W / "syn.rootfs.wic.gz", (DEPLOY / "fip-smarc-rzg3l.bin").read_bytes(), (DEPLOY / f"{CM33}.bin").read_bytes())
fake = FakeUBoot(W / "tftp", W / "emmc.img", state="prompt", tftp_rate=4 << 20)
m, s_ = pty.openpty()
proc = subprocess.Popen([sys.executable, str(TOOL), "rootfs", "--image", str(img), "--tftp-root", str(W / "tftp"),
                         "--log-dir", str(W / "logs"), "--wait", "30", "--skip-host-checks", "--port", fake.port, "--chunk-mb", "1"],
                        stdout=s_, stderr=s_, env={**os.environ, "COLUMNS": "140"})
os.close(s_)
raw = b""
while True:
    r, _, _ = select.select([m], [], [], 0.5)
    if r:
        try:
            chunk = os.read(m, 65536)
        except OSError:
            break
        if not chunk:
            break
        raw += chunk
    elif proc.poll() is not None:
        break
proc.wait(timeout=60)
fake.stop = True
text = raw.decode(errors="replace")
(W / "tty-output.txt").write_text(text)
check(proc.returncode == 0, "evk-flash rootfs on a terminal: OK")
check("\r\x1b[K" in text and "█" in text and "全部约剩" in text, "terminal: live bar with time left for the whole run")
check("[4/7] 写入 rootfs" in text and "[7/7] 重启并确认启动" in text, "terminal: numbered steps")
check("Linux 登录提示" in text and "U-Boot 2024.07" in text, "boot milestones are shown")
r = subprocess.run([sys.executable, str(TOOL), "status", "--log-dir", str(W / "logs")], capture_output=True, text=True)
check("状态：已完成" in r.stdout and "[7/7]" in r.stdout, f"evk-flash status shows the finished run ({r.stdout[-200:]})")

shutil.rmtree(W, ignore_errors=True)
print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
