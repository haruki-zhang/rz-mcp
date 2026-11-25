"""Fake RZ/G3L SMARC EVK on a pty, for testing workflow/board without a board.

What a person does is explicit: power_on(mode) with mode = "scif" (BootROM SCIF download), "emmc" or "qspi"
(BL2 -> U-Boot from that medium). The tests call it from an operator thread when the tool prints its
">>> 需要你操作" request. Strings come from the EVK's console log of 2026-11-25 (BootROM, Flash Writer V1.00
EM_W/SUP/em_secsd, U-Boot) and, for XLS2 (not yet seen on the board), from the Start-up Guide 4.4.

Storage is sparse files: `sd` = mmc 0 (eMMC user area when mmc_type="MMC"), `boot1` = eMMC boot partition 1,
`qspi` = the 16 MiB QSPI flash. ext_csd keeps what em_secsd set.
"""
import os
import pty
import re
import select
import threading
import time
import tty
import zlib
from pathlib import Path

SD_INFO = ("Device: sd@11c00000\nManufacturer ID: 3\nOEM: 5344\nName: SC64G \nBus Speed: 50000000\n"
           "Mode: SD High Speed (50MHz)\nRd Block Len: 512\nSD version 3.0\nHigh Capacity: Yes\n"
           "Capacity: 59.5 GiB\nBus Width: 4-bit\nErase Group Size: 512 Bytes")
MMC_INFO = ("Device: sd@11c00000\nManufacturer ID: 13\nOEM: 4e\nName: 0IM20F \nBus Speed: 50000000\n"   # EVK
            "Mode: MMC High Speed (52MHz)\nRd Block Len: 512\nMMC version 5.1\nHigh Capacity: Yes\n"
            "Capacity: 59.3 GiB\nBus Width: 8-bit\nErase Group Size: 512 KiB\nHC WP Group Size: 16 MiB\n"
            "User Capacity: 59.3 GiB WRREL\nBoot Capacity: 31.5 MiB ENH\nRPMB Capacity: 4 MiB ENH")
BOOTROM = "\x00\nSCI Download mode (Normal SCI boot)\n\n-- Load Program to SRAM  ---------------\n"   # EVK
FW_BANNER = "\nFlash writer for RZ/G3L Series V1.00 Jul.07,2026\n Product Code : RZ/G3L\n>"               # EVK
EMW_MENU = ("EM_W Start --------------\n---------------------------------------------------------\n"
            "Please select,eMMC Partition Area.\n 0:User Partition Area   : 62160896 KBytes\n"
            "  eMMC Sector Cnt : H'0 - H'0768FFFF\n 1:Boot Partition 1      : 32256 KBytes\n"
            "  eMMC Sector Cnt : H'0 - H'0000FBFF\n 2:Boot Partition 2      : 32256 KBytes\n"
            "  eMMC Sector Cnt : H'0 - H'0000FBFF\n---------------------------------------------------------\n"
            "  Select area(0-2)>")
SHELL = "root@smarc-rzg3l:~# "
LOGIN = "smarc-rzg3l login: "
UBOOT_BOOT = ("\nNOTICE:  BL2: v2.10.5(release):2.10.5/rzg3l_1.0.1-dirty\nNOTICE:  BL2: Built : 17:45:57, May  6 2026\n"
              "NOTICE:  BL31: v2.10.5(release):2.10.5/rzg3l_1.0.1-dirty\n\nU-Boot 2024.07 (Jun 22 2026 - 12:52:03 +0000)\n\n"
              "DRAM:  1.9 GiB\nMMC:   sd@11c00000: 0, sd@11c10000: 1\nNet:   \n"
              "Error: ethernet@11c40000 No valid MAC address found.\nError: ethernet@11c30000 No valid MAC address found.\n"
              "No ethernet found.\nHit any key to stop autoboot:  0 ")


def srec_records(lines):
    for line in lines:
        alen = {"1": 2, "2": 3, "3": 4}.get(line[1:2])
        if alen:
            raw = bytes.fromhex(line[2:])
            yield int.from_bytes(raw[1:1 + alen], "big"), raw[1 + alen:-1]


def sparse(path: Path | None, size: int) -> None:
    if path:
        with open(path, "wb") as f:
            f.truncate(size)


class FakeUBoot:
    def __init__(self, tftp_root: Path, sd: Path, sd_size: int = 2 << 30, eth_ok: str = "eth1", mmc_type: str = "MMC",
                 state: str = "off", corrupt_first_tftp: bool = False, boot1: Path | None = None,
                 qspi: Path | None = None, no_mac: bool = True, linux_cmds: dict | None = None):
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.port, self._slave = os.ttyname(slave), slave
        self.tftp_root, self.sd, self.eth_ok, self.mmc_type = tftp_root, sd, eth_ok, mmc_type
        self.boot1, self.qspi = boot1, qspi
        sparse(sd, sd_size)
        sparse(boot1, 0xFC00 * 512)
        sparse(qspi, 16 << 20)
        self.ext_csd = {0xB1: 0x00, 0xB3: 0x01}                      # EVK, before em_secsd
        self.no_mac = no_mac
        self.corrupt_next = corrupt_first_tftp
        self.env, self.mem, self.writes, self.reads = {}, {}, 0, 0
        self.line, self.stop, self.prev = "", False, ""
        self.fw = {"step": "cmd", "lines": []}
        self.linux_cmds = linux_cmds or {}      # shell command -> (output, exit status)
        self.state = state                    # off | bootrom | fw | countdown | prompt | rebooting | login | shell
        if state == "prompt":
            self.env = self._uboot_env()
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    # ------------------------------------------------------------ the operator
    def power_on(self, mode: str) -> None:
        with self.lock:
            self.line = ""
            if mode == "scif":
                self.fw = {"step": "mot", "lines": []}
                self.state = "bootrom"
                self.out(BOOTROM)
            else:
                self.env = self._uboot_env()
                self.state, self.countdown = "countdown", time.time()
                self.out(UBOOT_BOOT)

    def _uboot_env(self) -> dict:
        return {} if self.no_mac else {"ethaddr": "aa:bb:cc:dd:ee:ff"}

    def out(self, s: str) -> None:
        os.write(self.master, s.replace("\n", "\r\n").encode())

    # ------------------------------------------------------------ main loop
    def _run(self) -> None:
        while not self.stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            data = os.read(self.master, 65536).decode(errors="replace") if r else ""
            with self.lock:
                if self.state in ("off", "rebooting"):
                    continue
                if self.state in ("login", "shell"):
                    self._linux(data)
                    continue
                if self.state in ("bootrom", "fw"):
                    self._flash_writer(data)
                    continue
                if self.state == "countdown":
                    if data:
                        self.out("\n=> ")
                        self.state = "prompt"
                    elif time.time() - self.countdown > 1.0:     # nobody stopped autoboot
                        self.out("\nStarting kernel ...\n\n" + LOGIN)
                        self.state = "login"
                    continue
                for ch in data:
                    if ch == "\x03":
                        self.line = ""
                        self.out("<INTERRUPT>\n=> ")
                    elif ch == "\r":
                        self.out("\n")
                        res = self.exec(self.line.strip())
                        self.line = ""
                        if self.state == "rebooting":
                            self.out(res + "\n")
                            threading.Timer(1.0, self._reboot).start()
                            break
                        self.out((res + "\n" if res else "") + "=> ")
                    else:
                        self.line += ch
                        self.out(ch)

    def _reboot(self) -> None:
        with self.lock:
            self.out(UBOOT_BOOT + "\nStarting kernel ...\n\n" + LOGIN)
            self.state = "login"

    # ------------------------------------------------------------ Linux (login prompt, root shell)
    def _linux(self, data: str) -> None:
        for ch in data:
            if ch == "\x03":
                self.line = ""
                self.out("^C\n" + (SHELL if self.state == "shell" else LOGIN))
            elif ch == "\r":
                line, self.line = self.line.strip(), ""
                self.out("\n")
                if self.state == "login":
                    if line == "root":
                        self.state = "shell"
                        self.out(SHELL)
                    else:
                        self.out(LOGIN)
                else:
                    self._shell(line)
                    if self.state != "shell":
                        break
            elif ch != "\n":
                self.line += ch
                self.out(ch)

    def _shell(self, line: str) -> None:
        m = re.match(r"(.*); echo (__RZ\d+__)\$\?$", line)
        cmd, mark = (m.group(1).strip(), m.group(2)) if m else (line, None)
        if cmd == "reboot":
            self.out("reboot: Restarting system\n")
            self.state = "rebooting"
            threading.Timer(1.0, self._reboot).start()
            return
        out, rc = self.linux_cmds.get(cmd, (f"sh: {cmd.split()[0]}: not found", 127)) if cmd else ("", 0)
        if out:
            self.out(out + "\n")
        if mark:
            self.out(f"{mark}{rc}\n")
        self.out(SHELL)

    # ------------------------------------------------------------ BootROM + Flash Writer
    def _flash_writer(self, data: str) -> None:
        fw = self.fw
        for ch in data:
            if fw["step"] == "clear":                  # XLS2 "Clear OK?(y/n)" takes one key
                if ch in "yY":
                    self.out("y\n>")
                    fw["step"] = "cmd"
                continue
            if ch < " " and ch not in "\r\n":
                continue                                 # control characters (probes) are ignored
            if ch not in "\r\n":
                self.line += ch
                self.prev = ch
                continue
            if ch == "\n" and self.prev == "\r":
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
                        self.out(FW_BANNER)
                        fw.update(step="cmd", lines=[])
                    else:
                        self._store(fw)
                continue
            if self.state == "bootrom":
                continue                                 # stray bytes before the download: ignored
            self.out(line + "\n")                        # echo
            step = fw["step"]
            if step == "cmd":
                u = line.upper()
                if u == "SUP":
                    self.out("Scif speed UP\nPlease change to 921.6Kbps baud rate setting of the terminal.\n")
                    fw["step"] = "sup"
                elif u == "EM_W":
                    self.out(EMW_MENU)
                    fw.update(step="area", kind="emmc")
                elif u == "XLS2":
                    self.out("===== Qspi writing of RZ/G3L Board Command =============\nLoad Program to Spiflash\n"
                             "Writes to any of SPI address.\n Dialog : AT25QL128A\nProgram Top Address & Qspi Save Address\n"
                             "===== Please Input Program Top Address ============\n  Please Input : H'")
                    fw.update(step="xprog", kind="qspi")
                elif line.lower() == "em_secsd":
                    self.out("  Please Input EXT_CSD Index(H'00 - H'1FF) :")
                    fw["step"] = "csd_idx"
                elif line:
                    self.out("command not found\n>")
                else:
                    self.out(">")
            elif step == "sup":
                self.out(">")
                fw["step"] = "cmd"
            elif step == "area":
                fw["area"] = int(line)
                self.out(f"-- Boot Partition {line} Program -----------------------------\n"
                         "Please Input Start Address in sector :")
                fw["step"] = "sector"
            elif step == "sector":
                fw["dest"] = int(line, 16) * 512
                self.out("Please Input Program Start Address : ")
                fw["step"] = "prog"
            elif step == "prog":
                fw["prog"] = int(line, 16)
                self.out("Work RAM (H'40000000-H'4FFFFFFF) Clear....\nplease send ! ('.' & CR stop load)\n")
                fw.update(step="srec", lines=[])
            elif step == "xprog":
                fw["prog"] = int(line, 16)
                self.out("===== Please Input Qspi Save Address ===\n  Please Input : H'")
                fw["step"] = "xsave"
            elif step == "xsave":
                fw["dest"] = int(line, 16)
                self.out("please send ! ('.' & CR stop load)\n")
                fw.update(step="srec", lines=[])
            elif step == "csd_idx":
                fw["idx"] = int(line, 16)
                self.out(f"  EXT_CSD[{fw['idx']:X}] = 0x{self.ext_csd.get(fw['idx'], 0):02X}\n"
                         "  Please Input Value(H'00 - H'FF) : ")
                fw["step"] = "csd_val"
            elif step == "csd_val":
                self.ext_csd[fw["idx"]] = int(line, 16)
                self.out(f"  EXT_CSD[{fw['idx']:X}] = 0x{self.ext_csd[fw['idx']]:02X}\n>")
                fw["step"] = "cmd"

    def _store(self, fw: dict) -> None:
        target = self.boot1 if fw["kind"] == "emmc" else self.qspi
        lo = hi = None
        with open(target, "r+b") as f:
            for addr, d in srec_records(fw["lines"]):
                off = fw["dest"] + addr - fw["prog"]
                f.seek(off)
                f.write(d)
                lo = off if lo is None else min(lo, off)
                hi = off + len(d) if hi is None else max(hi, off + len(d))
        if fw["kind"] == "emmc":
            self.out("SAVE -FLASH.......\nEM_W Complete!\n>")
            fw.update(step="cmd", lines=[])
        else:
            self.out("Erase SPI Flash memory...\nErase Completed\nWrite to SPI Flash memory.\n"
                     "======= Qspi Save Information =================\n"
                     f" SpiFlashMemory Stat Address : H'{lo:08X}\n SpiFlashMemory End Address : H'{hi - 1:08X}\n"
                     "===========================================================\n"
                     f" SPI Data Clear(H'FF) Check : H'{fw['dest']:08X}-0000FFFF,Clear OK?(y/n)")
            fw.update(step="clear", lines=[])

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
            self.env = self._uboot_env()
            return "## Resetting to default environment"
        if a[0] == "saveenv":
            return "Saving Environment to MMC... Writing to MMC(0)... OK"
        if a[0] == "reset":
            self.state = "rebooting"
            return "resetting ..."
        if a[0] == "ping":
            if "ethaddr" not in self.env:
                return ("Error: ethernet@11c40000 No valid MAC address found.\n"
                        "Error: ethernet@11c30000 No valid MAC address found.\nNo ethernet found.")
            ok_dev = "ethernet@11c40000" if self.eth_ok == "eth1" else "ethernet@11c30000"
            act = self.env.get("ethact")
            if act in (self.eth_ok, ok_dev) or (act == "eth0" and self.eth_ok == "eth1"):  # U-Boot moves on
                self.env["ethact"] = ok_dev
                return f"Using {ok_dev} device\nhost {a[1]} is alive"
            return f"Using {act} device\n\nARP Retry count exceeded; starting again\nping failed; host {a[1]} is not alive"
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
            return (f"Using ethernet@11c40000 device\nTFTP from server 192.168.50.1; our IP address is 192.168.50.2\n"
                    f"Filename '{a[2]}'.\nLoad address: {addr:#x}\nLoading: #################\n\t 1 MiB/s\ndone\n"
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
