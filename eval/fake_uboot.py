"""Fake RZ/G3L board on a pty, for testing workflow/board without a board: BootROM SCIF download mode,
Flash Writer (SUP, EM_W, em_secsd) and U-Boot. Strings follow workflow/board/evk.py's sources.

The storage is sparse files: `sd` is the module microSD0 (U-Boot mmc 0 when mmc_type="SD") or the eMMC user
area (mmc_type="MMC"); `boot1` is eMMC boot partition 1. ext_csd keeps what em_secsd set.
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
EMW_MENU = ("EM_W Start --------------\n---------------------------------------------------------\n"
            "Please select,eMMC Partition Area.\n 0:User Partition Area   : 62160896 KBytes\n"
            "  eMMC Sector Cnt : H'0 - H'0768FFFF\n 1:Boot Partition 1      : 32256 KBytes\n"
            "  eMMC Sector Cnt : H'0 - H'0000FBFF\n 2:Boot Partition 2      : 32256 KBytes\n"
            "  eMMC Sector Cnt : H'0 - H'0000FBFF\n---------------------------------------------------------\n"
            "  Select area(0-2)>")


def srec_records(lines):
    for line in lines:
        alen = {"1": 2, "2": 3, "3": 4}.get(line[1:2])
        if alen:
            raw = bytes.fromhex(line[2:])
            yield int.from_bytes(raw[1:1 + alen], "big"), raw[1 + alen:-1]


class FakeUBoot:
    def __init__(self, tftp_root: Path, sd: Path, sd_size: int, eth_ok: str = "eth0", mmc_type: str = "SD",
                 at_prompt: bool = False, corrupt_first_tftp: bool = False, boot_after_verify: bool = False,
                 scif: bool = False, boot1: Path | None = None):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.port, self._slave = os.ttyname(slave), slave
        self.tftp_root, self.sd, self.eth_ok, self.mmc_type = tftp_root, sd, eth_ok, mmc_type
        with open(sd, "wb") as f:
            f.truncate(sd_size)
        self.boot1 = boot1
        if boot1:
            with open(boot1, "wb") as f:
                f.truncate(0xFC00 * 512)
        self.ext_csd = {0xB1: 0x02, 0xB3: 0x09}
        self.mode = "scif" if scif else "uboot"
        self.state = "prompt" if at_prompt else "off"
        self.corrupt_next = corrupt_first_tftp
        self.boot_after_verify = boot_after_verify
        self.env, self.mem, self.writes, self.reads = {"ethact": "eth0"}, {}, 0, 0
        self.line, self.stop, self.ready, self.reset_at = "", False, 0.0, 0.0
        self.fw = None           # Flash Writer dialog state
        threading.Thread(target=self._run, daemon=True).start()

    def out(self, s: str) -> None:
        os.write(self.master, s.replace("\n", "\r\n").encode())

    # ------------------------------------------------------------ main loop
    def _run(self) -> None:
        last = time.time()
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            data = os.read(self.master, 65536).decode(errors="replace") if r else ""
            now = time.time()
            if data:
                last = now
            if self.state == "off" and self.mode == "scif" and not self.ready:
                self.ready = now                     # SCIF phase: the tool sends nothing before "please send !"
            if self.state == "bootrom" and not self.fw["lines"] and now - self.banner_at > 3:
                self.state = "off"                   # a person presses reset again: BootROM prints its banner again
            if self.state == "off":
                if data and not self.ready:              # the tool opened the console and probed it
                    self.ready = now
                if self.ready and now - self.ready > 1.0:   # ...and a person reacted to its request: power on
                    if self.mode == "scif":
                        self.out("\x00\nSCI Download mode (Normal SCI boot)\n\n"    # as the EVK printed (2026-11-25)
                                 "-- Load Program to SRAM  ---------------\n")
                        self.state, self.fw, self.banner_at = "bootrom", {"step": "mot", "lines": []}, now
                    else:
                        self.out("\nNOTICE:  BL2: v2.10.4(release)\n\nU-Boot 2024.07 (Nov 25 2026)\n\n"
                                 "Hit any key to stop autoboot:  0 ")
                        self.state, self.countdown = "countdown", now
                continue
            if self.state in ("bootrom", "fw"):
                self._flash_writer(data)
                continue
            if self.state == "countdown":
                if data:
                    self.out("\n=> ")
                    self.state = "prompt"
                elif now - self.countdown > 1.0:
                    self.out("\nStarting kernel ...\n")
                    self.state = "linux"
                continue
            if self.state == "resetting":
                if now - self.reset_at > 1.0:
                    self.out("\nNOTICE:  BL2: v2.10.4(release)\n\nU-Boot 2024.07\nStarting kernel ...\n\n"
                             "smarc-rzg3l login: ")
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
                    if self.state == "resetting":
                        self.out(res + "\n")
                        break
                    self.out((res + "\n" if res else "") + "=> ")
                    self.line = ""
                else:
                    self.line += ch
                    self.out(ch)

    # ------------------------------------------------------------ BootROM + Flash Writer
    def _flash_writer(self, data: str) -> None:
        fw = self.fw
        if fw.get("done") and "\x03" in data:          # the tool waits for eMMC boot: a person power-cycles
            self.mode, self.mmc_type, self.state, self.ready = "uboot", "MMC", "off", time.time()
            return
        for ch in data:
            if ch not in "\r\n":
                self.line += ch
                self.prev = ch
                continue
            if ch == "\n" and getattr(self, "prev", "") == "\r":
                self.prev = ch
                continue                                 # CR LF is one line end
            self.prev = ch
            line, self.line = self.line.strip(), ""
            if fw["step"] in ("mot", "srec"):
                if not line:
                    continue
                fw["lines"].append(line)
                if line[:2] in ("S7", "S8", "S9"):
                    if fw["step"] == "mot":
                        self.state = "fw"
                        self.out("\nFlash writer for RZ/G3L Series V1.00 Jul.07,2026\n  Product Code : RZ/G3L\n>")
                    else:
                        with open(self.boot1, "r+b") as f:
                            for addr, d in srec_records(fw["lines"]):
                                f.seek(fw["sector"] * 512 + addr - fw["prog"])
                                f.write(d)
                        self.out("SAVE -FLASH.......\nEM_W Complete!\n>")
                    fw.update(step="cmd", lines=[])
                continue
            if ch == "\r":
                self.out(line + "\n")                  # echo
            if fw["step"] == "cmd":
                if line.upper() == "SUP":
                    self.out("Scif speed UP\nPlease change to 921.6Kbps baud rate setting of the terminal.")
                    fw["step"] = "sup"
                elif line.upper() == "EM_W":
                    self.out(EMW_MENU)
                    fw["step"] = "area"
                elif line.lower() == "em_secsd":
                    self.out("Please Input EXT_CSD Index(H'00 - H'1FF) : ")
                    fw["step"] = "csd_idx"
                elif line:
                    self.out("command not found\n>")
                else:
                    self.out(">")
            elif fw["step"] == "sup":
                self.out(">")
                fw["step"] = "cmd"
            elif fw["step"] == "area":
                fw["area"] = int(line)
                self.out(f"-- Boot Partition {line} Program -----------------------------\n"
                         "Please Input Start Address in sector :")
                fw["step"] = "sector"
            elif fw["step"] == "sector":
                fw["sector"] = int(line, 16)
                self.out("Please Input Program Start Address : ")
                fw["step"] = "prog"
            elif fw["step"] == "prog":
                fw["prog"] = int(line, 16)
                self.out("Work RAM(H'40000000-H'4FFFFFFF) Clear....\nplease send ! ('.' & CR stop load)\n")
                fw.update(step="srec", lines=[])
            elif fw["step"] == "csd_idx":
                fw["idx"] = int(line, 16)
                self.out(f"EXT_CSD[{fw['idx']:X}] = 0x{self.ext_csd.get(fw['idx'], 0):02X}\n"
                         "Please Input Value(H'00 - H'FF) : ")
                fw["step"] = "csd_val"
            elif fw["step"] == "csd_val":
                self.ext_csd[fw["idx"]] = int(line, 16)
                self.out(f"EXT_CSD[{fw['idx']:X}] = 0x{self.ext_csd[fw['idx']]:02X}\n>")
                fw["step"] = "cmd"
                if fw["idx"] == 0xB3:
                    fw["done"] = True

    # ------------------------------------------------------------ U-Boot
    def exec(self, line: str) -> str:
        a = line.split()
        if not a:
            return ""
        if a[0] == "printenv":
            return "\n".join(f"{k}={self.env[k]}" if k in self.env else f'## Error: "{k}" not defined' for k in a[1:])
        if a[0] == "setenv":
            self.env[a[1]] = a[2] if len(a) > 2 else ""
            return ""
        if a[:3] == ["env", "default", "-a"]:
            return "## Resetting to default environment"
        if a[0] == "saveenv":
            return "Saving Environment to MMC... Writing to MMC(0)... OK"
        if a[0] == "reset":
            self.state, self.reset_at = "resetting", time.time()
            return "resetting ..."
        if a[0] == "ping":
            dev = "Using ethernet@11c30000 device\n"
            if self.env.get("ethact") == self.eth_ok and a[1] == self.env.get("serverip") == "192.168.50.1":
                return dev + f"host {a[1]} is alive"
            return dev + f"\nARP Retry count exceeded; starting again\nping failed; host {a[1]} is not alive"
        if a[:2] == ["mmc", "dev"]:
            part = f"(part {a[3]})" if len(a) > 3 else ""
            return f"switch to partitions #0, OK\nmmc{a[2]}{part} is current device"
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
            return (f"Using ethernet@11c30000 device\nTFTP from server 192.168.50.1; our IP address is 192.168.50.2\n"
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
