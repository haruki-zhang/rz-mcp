# Run: /usr/bin/python3 eval/test_verify.py   (system python3: needs pyserial)
"""Offline tests for workflow/board/evk-verify against eval/fake_uboot.py (board at the Linux login prompt)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RZ / "eval"))
from fake_uboot import FakeUBoot  # noqa: E402

TOOL = RZ / "workflow" / "board" / "evk-verify"
W = Path("/tmp/verify-tests")
fails = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    if not c:
        fails.append(m)


CMDS = {"uname -r": ("6.12.46-cip8-yocto-standard", 0),
        "ip -4 -o addr show eth0": ("2: eth0    inet 192.168.50.123/24 brd 192.168.50.255 scope global eth0", 0),
        "cat /proc/cmdline": ("rw rootwait root=/dev/mmcblk0p2", 0),
        "false": ("", 1),
        "ip -4 -o addr show evil": ("2: evil    inet 1.2.3.4;touch_/tmp/verify-tests/pwned/24", 0)}

PLAN = '''
[[check]]
id = "V-1"
title = "kernel version"
requirement = "1"
where = "linux"
run = "uname -r"
expect = "^6\\\\.12\\\\."

[[check]]
id = "V-2"
title = "eth0 has an address on the direct link"
where = "linux"
run = "ip -4 -o addr show eth0"
expect = "inet 192\\\\.168\\\\.50\\\\."
capture = "inet ([0-9.]+)/"
capture_as = "ip"

[[check]]
id = "V-3"
title = "the host reaches the board"
where = "host"
run = "echo reach ${ip}"
expect = "^reach 192\\\\.168\\\\.50\\\\.123$"

[[check]]
id = "V-4"
title = "quiet boot"
where = "linux"
run = "cat /proc/cmdline"
expect = "quiet"

[[check]]
id = "V-5"
title = "exit status is checked"
where = "linux"
run = "false"
expect = ""
expect_rc = 1

[[check]]
id = "V-6"
title = "boot time"
where = "boot"
max_seconds = 10
'''


def poc(name: str, plan: str) -> Path:
    p = W / name
    (p / "design").mkdir(parents=True)
    (p / "design" / "verify.toml").write_text(plan)
    return p


def run(p: Path, fake: FakeUBoot) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-u", str(TOOL), str(p), "--port", fake.port, "--wait", "20"],
                       capture_output=True, text=True, timeout=300)
    fake.stop = True
    return r.returncode, r.stdout + r.stderr


shutil.rmtree(W, ignore_errors=True)
W.mkdir()

p = poc("main", PLAN)
f = FakeUBoot(W, W / "sd.img", 1 << 20, state="login", linux_cmds=CMDS)
rc, out = run(p, f)
rep = (p / "design" / "03-verification.md").read_text()
res = json.loads(next((p / "design" / "verify-logs").glob("*/results.json")).read_text())["results"]
by = {r["id"]: r for r in res}
check(rc == 1 and "1 项未通过（5/6）" in rep, f"one failing check -> exit 1, report says 5/6 ({out[-300:]})")
check(by["V-1"]["pass"] and by["V-2"]["pass"] and by["V-5"]["pass"], "linux checks: regex, capture, expected exit status")
check(by["V-2"]["captured"] == {"ip": "192.168.50.123"} and by["V-3"]["pass"] and by["V-3"]["command"] == "echo reach 192.168.50.123",
      "a value captured on the board is used in a host command")
check(not by["V-4"]["pass"] and "| **FAIL** |" in rep, "a missing pattern fails and is marked in the report")
check(by["V-6"]["pass"] and by["V-6"]["metrics"]["login"] <= 10 and set(by["V-6"]["metrics"]) == {"u_boot", "kernel", "login"},
      "boot check: reboot timed to U-Boot, kernel and login")
check("## 原始输出" in rep and "$ uname -r" in rep, "report keeps the raw output of each command")

evil_plan = ('[[check]]\nid = "E-1"\ntitle = "capture"\nwhere = "linux"\nrun = "ip -4 -o addr show evil"\nexpect = "inet"\n'
             'capture = "inet (\\\\S+)/"\ncapture_as = "ip"\n\n[[check]]\nid = "E-2"\ntitle = "host"\nwhere = "host"\n'
             'run = "echo ${ip}"\nexpect = "."\n')
p = poc("evil", evil_plan)
f = FakeUBoot(W, W / "sd2.img", 1 << 20, state="shell", linux_cmds=CMDS)
rc, out = run(p, f)
check(rc == 1 and not (W / "pwned").exists() and "characters outside" in (p / "design" / "03-verification.md").read_text(),
      "a captured value with shell syntax is rejected; the host command never runs")
log = next((p / "design" / "verify-logs").glob("*/console.log")).read_text()
check("login:" not in log and "root@smarc-rzg3l" in log, "board already at the shell: no login needed")

p = poc("tmpl", (RZ / "workflow/templates/design/verify.toml").read_text())
f = FakeUBoot(W, W / "sd3.img", 1 << 20, state="login", linux_cmds=CMDS)
rc, out = run(p, f)
check(rc != 0 and "still the template" in out, "the unfilled template is refused")

shutil.rmtree(W, ignore_errors=True)
print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
