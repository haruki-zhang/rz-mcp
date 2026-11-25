"""RZ/G3L SMARC EVK console harness: serial console with logging, and U-Boot helpers.

Output formats are matched against U-Boot at the BSP revision (renesas-u-boot-cip 16e6324c):
  common/autoboot.c "Hit any key to stop autoboot", cmd/net.c "host %s is alive",
  net/net.c "Bytes transferred = %u (%x hex)", common/hash.c "CRC32 for ... ==> %08lx",
  cmd/mmc.c "%d blocks written: OK" / "SD version" vs "MMC version".
Needs pyserial (system python3 has it).
"""
import re
import sys
import time

import serial

PROMPT = "=> "
PORT = "/dev/rzg3l-evk-console"       # udev symlink from setup-host.sh
LOADADDR = 0x58000000                  # CONFIG_SYS_LOAD_ADDR of smarc-rzg3l_defconfig; DDR is 2 GB from 0x40000000
HOST_IP, BOARD_IP, NETMASK = "192.168.50.1", "192.168.50.2", "255.255.255.0"


def human(msg: str) -> None:
    """A step only a person can do (power, switches, cables). Printed loudly; the tool then waits on the console."""
    print(f"\n>>> 需要你操作：{msg}\n", flush=True)


class BoardError(RuntimeError):
    pass


class Console:
    def __init__(self, port: str = PORT, baud: int = 115200, log: str | None = None, echo: bool = False):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.buf = ""
        self.echo = echo
        self.log = open(log, "a", encoding="utf-8", errors="replace") if log else None

    def close(self) -> None:
        self.ser.close()
        if self.log:
            self.log.close()

    def _pump(self) -> bool:
        data = self.ser.read(65536)
        if not data:
            return False
        s = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        self.buf += s
        if self.log:
            self.log.write(s)
            self.log.flush()
        if self.echo:
            sys.stdout.write(s)
            sys.stdout.flush()
        return True

    def send(self, s: str) -> None:
        self.ser.write(s.encode())

    def expect(self, patterns: list[str], timeout: float) -> tuple[int, str]:
        """Wait until one regex matches the buffered output; consume up to the match and return (index, text)."""
        deadline = time.time() + timeout
        while True:
            for i, p in enumerate(patterns):
                m = re.search(p, self.buf)
                if m:
                    text, self.buf = self.buf[:m.end()], self.buf[m.end():]
                    return i, text
            if time.time() > deadline:
                raise TimeoutError(f"no {patterns!r} within {timeout:.0f}s; last output: {self.buf[-300:]!r}")
            self._pump()

    # ---- U-Boot ----
    def at_prompt(self, wait: float = 1.0) -> bool:
        self.buf = ""
        self.send("\x03")
        try:
            self.expect([re.escape(PROMPT) + r"$"], wait)
            return True
        except TimeoutError:
            return False

    def stop_autoboot(self, wait: float) -> None:
        """Get to the U-Boot prompt. If the board is not already there, a person powers it on or resets it;
        from the first boot output on, keep pressing a key until the prompt appears (also works with bootdelay=0)."""
        if self.at_prompt():
            return
        human("给 EVK 上电，或者按一下复位键。工具会自动打断 U-Boot 的自动启动。")
        deadline = time.time() + wait
        seen = bool(self.buf.strip())     # output already arrived during at_prompt(): the board is booting now
        while time.time() < deadline:
            if self._pump():
                seen = True
            if seen:
                self.send(" ")
                if re.search(re.escape(PROMPT) + r"\s*$", self.buf):
                    time.sleep(0.2)
                    if self.at_prompt():
                        return
            else:
                time.sleep(0.05)
        raise TimeoutError("没有等到 U-Boot 提示符。确认串口线（Carrier II 的 micro-USB 'UART Debug' 口）、"
                           "电源，以及 SW_MODE 设成了板子上现有镜像能启动的模式")

    def cmd(self, line: str, timeout: float = 10) -> str:
        """Run one U-Boot command; return its output (without the echoed command line and the prompt)."""
        self.buf = ""
        self.send(line + "\r")
        _, text = self.expect([r"\n" + re.escape(PROMPT) + r"$"], timeout)
        out = text.split("\n", 1)[1] if "\n" in text else ""
        return out[: -len(PROMPT)].rstrip("\n") if out.endswith(PROMPT) else out

    def mmc_select(self, dev: int) -> dict:
        out = self.cmd(f"mmc dev {dev}")
        if "is current device" not in out:
            raise BoardError(f"mmc dev {dev} failed: {out!r}")
        info = self.cmd("mmc info")
        m = re.search(r"^(SD|MMC) version", info, re.M)
        cap = re.search(r"^Capacity: ([\d.]+) ([KMGT])iB", info, re.M)
        if not m or not cap:
            raise BoardError(f"cannot read mmc info: {info!r}")
        size = float(cap.group(1)) * 1024 ** " KMGT".index(cap.group(2))
        return {"type": m.group(1), "capacity": int(size), "info": info}

    def net_setup(self) -> str:
        """Static addresses (no DHCP needed). Try eth0, then eth1 (the two RJ45 jacks of Carrier II)."""
        for k, v in (("ipaddr", BOARD_IP), ("serverip", HOST_IP), ("netmask", NETMASK)):
            self.cmd(f"setenv {k} {v}")
        for eth in ("eth0", "eth1"):
            self.cmd(f"setenv ethact {eth}")
            out = self.cmd(f"ping {HOST_IP}", timeout=20)
            if f"host {HOST_IP} is alive" in out:
                dev = re.search(r"Using (\S+) device", out)
                return f"{eth} ({dev.group(1) if dev else '?'})"
        raise BoardError("ping 主机失败：确认 USB 网卡接在 Carrier II 的 RJ45 口上，并且主机端 evk-direct 已经连上"
                         "（nmcli con show evk-direct）")

    def tftp(self, addr: int, path: str, size: int) -> None:
        out = self.cmd(f"tftpboot {addr:#x} {path}", timeout=max(60, size / 2e5))
        m = re.search(r"Bytes transferred = (\d+)", out)
        if not m or int(m.group(1)) != size:
            raise BoardError(f"tftp {path}: {out[-300:]!r}")

    def crc32(self, addr: int, size: int) -> int:
        out = self.cmd(f"crc32 {addr:#x} {size:#x}", timeout=60)
        m = re.search(r"==> ([0-9a-fA-F]{8})", out)
        if not m:
            raise BoardError(f"crc32: {out!r}")
        return int(m.group(1), 16)

    def mmc_rw(self, op: str, addr: int, blk: int, cnt: int) -> None:
        out = self.cmd(f"mmc {op} {addr:#x} {blk:#x} {cnt:#x}", timeout=max(60, cnt * 512 / 2e6))
        word = "written" if op == "write" else "read"
        m = re.search(rf"(\d+) blocks {word}: (OK|ERROR)", out)
        if not m or m.group(2) != "OK" or int(m.group(1)) != cnt:
            raise BoardError(f"mmc {op} block {blk:#x} count {cnt:#x}: {out!r}")
