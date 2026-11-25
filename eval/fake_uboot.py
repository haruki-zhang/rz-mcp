"""Fake RZ/G3L U-Boot on a pty, for testing workflow/board without a board.

Output strings follow renesas-u-boot-cip 16e6324c (see workflow/board/evk.py). The "SD card" is a sparse file.
"""
import os
import pty
import select
import threading
import time
import tty
import zlib
from pathlib import Path

SD_INFO = ("Device: mmc@11c00000\nManufacturer ID: 3\nOEM: 5344\nName: SC64G \nBus Speed: 50000000\n"
           "Mode: SD High Speed (50MHz)\nRd Block Len: 512\nSD version 3.0\nHigh Capacity: Yes\n"
           "Capacity: 59.5 GiB\nBus Width: 4-bit\nErase Group Size: 512 Bytes")
MMC_INFO = ("Device: mmc@11c00000\nManufacturer ID: 13\nOEM: 14e\nName: Q2J55L \nBus Speed: 52000000\n"
            "Mode: MMC High Speed (52MHz)\nRd Block Len: 512\nMMC version 5.1\nHigh Capacity: Yes\n"
            "Capacity: 58.2 GiB\nBus Width: 8-bit\nErase Group Size: 512 KiB")


class FakeUBoot:
    def __init__(self, tftp_root: Path, sd: Path, sd_size: int, eth_ok: str = "eth0", mmc_type: str = "SD",
                 at_prompt: bool = False, corrupt_first_tftp: bool = False, boot_after_verify: bool = False):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.port, self._slave = os.ttyname(slave), slave
        self.tftp_root, self.sd, self.eth_ok, self.mmc_type = tftp_root, sd, eth_ok, mmc_type
        with open(sd, "wb") as f:
            f.truncate(sd_size)
        self.state = "prompt" if at_prompt else "off"
        self.corrupt_next = corrupt_first_tftp
        self.boot_after_verify = boot_after_verify
        self.env, self.mem, self.writes, self.reads = {"ethact": "eth0"}, {}, 0, 0
        self.line, self.stop, self.ready = "", False, 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def out(self, s: str) -> None:
        os.write(self.master, s.replace("\n", "\r\n").encode())

    def _run(self) -> None:
        last = time.time()
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            data = os.read(self.master, 4096).decode(errors="replace") if r else ""
            now = time.time()
            if data:
                last = now
            if self.state == "off":
                if data and not self.ready:              # the tool opened the console and probed it
                    self.ready = now
                if self.ready and now - self.ready > 1.0:   # ...and a person reacted to its request: power on
                    self.out("\nNOTICE:  BL2: v2.10.4(release)\n\nU-Boot 2024.07 (Nov 25 2026)\n\n"
                             "Hit any key to stop autoboot:  2 ")
                    self.state, self.countdown = "countdown", now
                continue
            if self.state == "countdown":
                if data:
                    self.out("\n=> ")
                    self.state = "prompt"
                elif now - self.countdown > 2:
                    self.out("\nStarting kernel ...\n")
                    self.state = "linux"
                continue
            if self.state == "linux":
                continue
            if self.boot_after_verify and self.reads and not data and now - last > 1.0:
                self.out("\nStarting kernel ...\n[    0.000000] Booting Linux\n\nsmarc-rzg3l login: ")
                self.state = "linux"
                continue
            for ch in data:
                if ch == "\x03":
                    self.line = ""
                    self.out("<INTERRUPT>\n=> ")
                elif ch == "\r":
                    self.out("\n")
                    res = self.exec(self.line.strip())
                    self.out((res + "\n" if res else "") + "=> ")
                    self.line = ""
                else:
                    self.line += ch
                    self.out(ch)

    def exec(self, line: str) -> str:
        a = line.split()
        if not a:
            return ""
        if a[0] == "setenv":
            self.env[a[1]] = a[2] if len(a) > 2 else ""
            return ""
        if a[0] == "ping":
            dev = "Using ethernet@11c20000 device\n"
            if self.env.get("ethact") == self.eth_ok and a[1] == self.env.get("serverip") == "192.168.50.1":
                return dev + f"host {a[1]} is alive"
            return dev + f"\nARP Retry count exceeded; starting again\nping failed; host {a[1]} is not alive"
        if a[:2] == ["mmc", "dev"]:
            return f"switch to partitions #0, OK\nmmc{a[2]} is current device"
        if a[:2] == ["mmc", "info"]:
            return SD_INFO if self.mmc_type == "SD" else MMC_INFO
        if a[0] == "tftpboot":
            addr, p = int(a[1], 16), self.tftp_root / a[2]
            if not p.exists():
                return "TFTP error: 'File not found' (1)\nNot retrying..."
            data = p.read_bytes()
            if self.corrupt_next:
                data, self.corrupt_next = bytes([data[0] ^ 1]) + data[1:], False
            self.mem[addr] = data
            return (f"Using ethernet@11c20000 device\nTFTP from server 192.168.50.1; our IP address is 192.168.50.2\n"
                    f"Filename '{a[2]}'.\nLoad address: {addr:#x}\nLoading: #################\n\t 9.8 MiB/s\ndone\n"
                    f"Bytes transferred = {len(data)} ({len(data):x} hex)")
        if a[0] == "crc32":
            addr, n = int(a[1], 16), int(a[2], 16)
            return f"CRC32 for {addr:08x} ... {addr + n - 1:08x} ==> {zlib.crc32(self.mem[addr][:n]) & 0xFFFFFFFF:08x}"
        if a[0] == "mmc" and a[1] in ("write", "read"):
            addr, blk, cnt = (int(x, 16) for x in a[2:5])
            with open(self.sd, "r+b") as f:
                f.seek(blk * 512)
                if a[1] == "write":
                    f.write(self.mem[addr][:cnt * 512])
                    self.writes += 1
                    return f"\nMMC write: dev # 0, block # {blk}, count {cnt} ... {cnt} blocks written: OK"
                self.mem[addr] = f.read(cnt * 512)
                self.reads += 1
                return f"\nMMC read: dev # 0, block # {blk}, count {cnt} ... {cnt} blocks read: OK"
        return f"Unknown command '{a[0]}' - try 'help'"
