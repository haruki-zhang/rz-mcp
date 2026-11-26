"""RZ/G3L SMARC EVK board harness: serial console with logging, Flash Writer and U-Boot helpers, image staging.

Output formats are matched against the sources the BSP uses:
  U-Boot (renesas-u-boot-cip 16e6324c): common/autoboot.c "Hit any key to stop autoboot", cmd/net.c
  "host %s is alive", net/net.c "Bytes transferred = %u (%x hex)", common/hash.c "CRC32 for ... ==> %08lx",
  cmd/mmc.c "%d blocks written: OK" / "SD version" vs "MMC version".
  BootROM and Flash Writer: Linux Start-up Guide R01US0858EJ0100 Rev.1.00-rc1, sections 4.3, 4.4 and 8.2
  ("please send !", "Flash writer for RZ/G3L", SUP, EM_W, em_secsd).
Needs pyserial (system python3 has it).

A script that opens the console (Console) is a board job: evk-watch can follow it. If the script sets no
progress.Progress of its own, the first output from the board creates one for the process: <log>.status.json and
<log>.status.events.jsonl next to the log (or data/board-runs/<time>-<script>/ without a log). Holding the real
console registers the job in data/board-runs/active.json; ask_human() is what evk-watch passes on to the user.
"""
import gzip
import hashlib
import json
import os
import re
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress as _progress  # noqa: E402

PROMPT = "=> "
SHELL_PROMPT = r"root@[\w.-]+:[^\n]*# $"   # debug-tweaks images log root in without a password
FW_PROMPT = r"\n>[>\s]*$"             # Flash Writer prompt; a trailing CR LF of a sent file can add a second ">"
PORT = "/dev/rzg3l-evk-console"       # udev symlink from setup-host.sh
LOADADDR = 0x58000000                  # CONFIG_SYS_LOAD_ADDR; U-Boot reports 896 MiB of DRAM from 0x40000000
HOST_IP, BOARD_IP, NETMASK = "192.168.50.1", "192.168.50.2", "255.255.255.0"
LOCAL_MACS = ("02:52:5a:47:33:4c", "02:52:5a:47:33:4d")   # locally administered ("RZG3L"), direct link only
SECTOR = 512


def human(msg: str) -> None:
    """A step only a person can do (power, switches, cables). Printed loudly; the tool then waits on the console."""
    print(f"\n>>> 需要你操作：{msg}\n", flush=True)


class BoardError(RuntimeError):
    pass


_AUTO: "_progress.Progress | None" = None     # the per-process Progress of a script that set none


def _auto_progress(log: str | None) -> "_progress.Progress":
    global _AUTO
    if _AUTO is None:
        if log:
            status = Path(log).with_suffix(".status.json")
        else:
            d = _progress.RZ / "data" / "board-runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(sys.argv[0]).stem or 'script'}"
            d.mkdir(parents=True, exist_ok=True)
            status = d / "status.json"
        _AUTO = _progress.Progress(status)
        _AUTO.set_plan([])
    return _AUTO


# ---------------------------------------------------------------- files on the host

def read_bmap(path: Path) -> dict:
    raw = path.read_bytes()
    # A bmap has no DTD; refusing DOCTYPE/ENTITY rules out XXE and entity expansion without defusedxml.
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        sys.exit(f"{path}: refusing a bmap with a DOCTYPE or ENTITY declaration")
    r = ET.fromstring(raw)
    if not r.get("version", "").startswith("2."):
        sys.exit(f"{path}: unsupported bmap version {r.get('version')}")
    ranges = []
    for e in r.find("BlockMap"):
        a, _, b = e.text.strip().partition("-")
        ranges.append((int(a), int(b or a), e.get("chksum")))
    return {"image_size": int(r.find("ImageSize").text), "block_size": int(r.find("BlockSize").text),
            "ranges": ranges, "checksum": r.find("ChecksumType").text.strip()}


def srec_decode(path: Path) -> tuple[int, bytes]:
    """Decode a Motorola S-record file (S1/S2/S3 data). Returns (lowest address, contiguous data); checksums checked."""
    recs = []
    for n, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line[0] != "S" or line[1] not in "0123456789":
            raise ValueError(f"{path}:{n}: not an S-record")
        raw = bytes.fromhex(line[2:])
        if raw[0] != len(raw) - 1 or (sum(raw) & 0xFF) != 0xFF:
            raise ValueError(f"{path}:{n}: bad length or checksum")
        alen = {"1": 2, "2": 3, "3": 4}.get(line[1])
        if alen:
            recs.append((int.from_bytes(raw[1:1 + alen], "big"), raw[1 + alen:-1]))
    if not recs:
        raise ValueError(f"{path}: no data records")
    lo = min(a for a, _ in recs)
    hi = max(a + len(d) for a, d in recs)
    buf = bytearray(b"\xff" * (hi - lo))
    for a, d in recs:
        buf[a - lo:a - lo + len(d)] = d
    return lo, bytes(buf)


def wic_read(image: Path, offset: int, length: int) -> bytes:
    """Read bytes of the uncompressed WIC (sequential gzip; meant for the first few MiB)."""
    with gzip.open(image, "rb") as f:
        f.read(offset)
        return f.read(length)


def wic_partitions(head: bytes) -> list[tuple[int, int]]:
    """(first, last) sector of every partition in a WIC's MBR, or GPT behind a protective MBR; [] without a
    partition table. `head` is the start of the image (wic_read); GPT entries beyond it are not seen."""
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        return []
    out = []
    for i in range(4):
        e = head[446 + 16 * i:462 + 16 * i]
        start, size = struct.unpack("<II", e[8:16])
        if e[4] == 0xEE:                                     # protective MBR: the table is the GPT at LBA 1
            h = head[512:1024]
            if h[:8] != b"EFI PART":
                return []
            lba, n, esz = struct.unpack("<QII", h[72:88])
            for k in range(n):
                o = lba * SECTOR + k * esz
                if o + esz > len(head):
                    break
                ent = head[o:o + esz]
                if ent[:16].strip(b"\0"):
                    out.append(struct.unpack("<QQ", ent[32:48]))
            return out
        if e[4] and size:
            out.append((start, start + size - 1))
    return out


def raw_region(head: bytes, sector: int) -> tuple[int, int] | None:
    """None if `sector` lies outside every partition (where a wks places raw firmware), else that partition."""
    return next(((a, b) for a, b in wic_partitions(head) if a <= sector <= b), None)


def stage_image(image: Path, bmap_path: Path, stage: Path, chunk_bytes: int) -> dict:
    """Check every mapped range of the image against the bmap sha256 and cut the mapped data into CRC32'd chunks."""
    bm = read_bmap(bmap_path)
    if bm["checksum"] != "sha256":
        sys.exit(f"unsupported checksum type {bm['checksum']}")
    man_path = stage / "manifest.json"
    if man_path.exists():
        man = json.loads(man_path.read_text())
        if all((stage / c["file"]).exists() for c in man["chunks"]):
            print(f"prepare: reusing {stage} ({len(man['chunks'])} chunks)")
            return man
    stage.mkdir(parents=True, exist_ok=True)
    bs, size = bm["block_size"], bm["image_size"]
    chunks, pos, t0 = [], 0, time.time()
    with gzip.open(image, "rb") as f:
        for first, last, chksum in bm["ranges"]:
            start, end = first * bs, min((last + 1) * bs, size)
            f.read(start - pos)                       # skip unmapped data (sequential gzip stream)
            data = f.read(end - start)
            pos = end
            if len(data) != end - start or hashlib.sha256(data).hexdigest() != chksum:
                sys.exit(f"image does not match the bmap at blocks {first}-{last}: wrong or damaged image")
            for off in range(0, len(data), chunk_bytes):
                piece = data[off:off + chunk_bytes]
                piece += b"\0" * (-len(piece) % SECTOR)   # only the image's last sector can be partial
                name = f"c{len(chunks):03d}.bin"
                (stage / name).write_bytes(piece)
                chunks.append({"file": name, "sector": (start + off) // SECTOR, "sectors": len(piece) // SECTOR,
                               "bytes": len(piece), "crc32": zlib.crc32(piece) & 0xFFFFFFFF})
    man = {"image": str(image), "bmap": str(bmap_path), "image_size": size, "chunks": chunks,
           "mapped_bytes": sum(c["bytes"] for c in chunks)}
    man_path.write_text(json.dumps(man, indent=1))
    print(f"prepare: {len(chunks)} chunks, {man['mapped_bytes'] / 2**20:.0f} MiB mapped of "
          f"{size / 2**20:.0f} MiB, sha256 of all {len(bm['ranges'])} bmap ranges OK ({time.time() - t0:.0f}s)")
    return man


# ---------------------------------------------------------------- console

class Console:
    MILESTONES = [(r"SCIF? Download mode[^\n]*", "BootROM：SCIF 下载模式"), (r"Flash writer for RZ/G3L[^\n]*", None),
                  (r"BL2: v[^\n]*", None), (r"BL31: v[^\n]*", None), (r"U-Boot 20\d\d\.\d\d[^\n]*", None),
                  (r"Starting kernel", "Linux 内核开始启动"), (r"[\w-]+ login: ", "Linux 登录提示")]

    def __init__(self, port: str = PORT, baud: int = 115200, log: str | None = None, echo: bool = False):
        try:   # exclusive: a second tool or a terminal program on the same console would corrupt the transfers
            self.ser = serial.Serial(port, baud, timeout=0.05, exclusive=True)
        except serial.SerialException as e:
            raise BoardError(f"cannot open {port}: {e} (另一个工具或终端程序正在使用串口？)") from e
        self.buf = ""
        self.echo = echo
        self.port = port
        self.log_path = log
        # registered as the active board job only on the real console (a test's fake board is a pty), or when
        # RZ_BOARD_ACTIVE points the registry somewhere else (tests)
        self.registers = bool(os.environ.get("RZ_BOARD_ACTIVE")) or os.path.realpath(port) == os.path.realpath(PORT)
        self._progress = None         # progress.Progress, set by the caller (else created at the first output)
        self.tick = None              # called while waiting (live progress)
        self.wait_label = None        # what we are waiting for (shown with the elapsed time)
        self.seen = set()             # milestones already shown
        self.recent = ""
        self.log = open(log, "a", encoding="utf-8", errors="replace") if log else None

    @property
    def progress(self):
        return self._progress

    @progress.setter
    def progress(self, p) -> None:
        self._progress = p
        if p is not None and self.registers:
            p.register(port=self.port, log=self.log_path)

    def _ensure_progress(self) -> None:
        if self._progress is None:
            self.progress = _auto_progress(self.log_path)

    def close(self) -> None:
        self.ser.close()
        if self.log:
            self.log.close()

    def baud(self, rate: int) -> None:
        self.ser.baudrate = rate

    def _pump(self) -> bool:
        data = self.ser.read(65536)
        if not data:
            return False
        self._ensure_progress()
        s = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
        self.buf += s
        if self.progress:
            self.progress.human_done()
            self.recent = (self.recent + s)[-4000:]
            for rx, label in self.MILESTONES:
                if rx not in self.seen:
                    m = re.search(rx, self.recent)
                    if m:
                        self.seen.add(rx)
                        self.progress.milestone(label or m.group(0).strip())
        if self.log:
            self.log.write(s)
            self.log.flush()
        if self.echo:
            sys.stdout.write(s)
            sys.stdout.flush()
        return True

    def say(self, text: str) -> None:
        self._ensure_progress()
        self.progress.note(text)

    def ask_human(self, msg: str) -> None:
        """Something only a person can do (switches, power, RESET, cards, cables). Printed loudly and recorded as
        an event: evk-watch passes it on. The board's next output marks it done."""
        self._ensure_progress()
        self.progress.human(msg)

    def result(self, ok: bool, msg: str = "") -> None:
        """End the job with its result (evk-watch prints it and exits). Optional: without it the job is closed
        when the process exits, as failed after an uncaught exception, else with no result."""
        self._ensure_progress()
        self.progress.end(ok, msg)

    def watch(self, label: str | None) -> None:
        """Show milestones again from here on (a new boot), and what we are waiting for."""
        self.seen, self.recent, self.wait_label = set(), "", label

    def send(self, s: str | bytes) -> None:
        self.ser.write(s.encode() if isinstance(s, str) else s)

    def send_file(self, path: Path) -> None:
        """Send a file (S-records) the way terminal software does, while keeping the log of what the board prints."""
        data = path.read_bytes()
        for off in range(0, len(data), 4096):
            self.ser.write(data[off:off + 4096])
            self._pump()
            if self.progress:
                self.progress.update(min(off + 4096, len(data)), len(data))
        if self.progress:
            self.progress.update(len(data), len(data))

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
            if self.tick:
                self.tick()
            elif self.progress and self.wait_label:
                self.progress.waiting(self.wait_label)

    # ---- BootROM SCIF download mode and Flash Writer ----
    def scif_download(self, mot: Path, wait: float, already_waiting: bool = False) -> str:
        """BootROM in SCIF download mode -> send Flash Writer -> its prompt. Returns the Flash Writer banner line.

        The EVK's BootROM prints "SCI Download mode (Normal SCI boot)" / "-- Load Program to SRAM ---" and then
        waits silently (seen on the board 2026-11-25); the Start-up Guide rc1 shows "SCIF Download mode" /
        "-- Load Program to SystemRAM ---" / "please send !". Both are accepted. already_waiting: the BootROM
        banner was printed before this run, so send at once."""
        if not already_waiting:
            self.buf = ""
            self.watch("等待 BootROM（SCIF 下载模式）的提示")
            self.expect([r"please send !", r"-- Load Program to S\w*RAM"], wait)
            try:                                   # the rc1 wording adds "please send !" after the Load line
                self.expect([r"please send !"], 1.0)
            except TimeoutError:
                pass
        if not self.progress:              # with a progress display the milestone line says it already
            self.say(f"BootROM: SCIF download mode; sending {mot.name} at 115200 bps ...")
        self.wait_label = None
        self.send_file(mot)
        self.wait_label = "等待 Flash Writer 启动"
        _, text = self.expect([r"Flash writer for RZ/G3L[^\n]*"], 120)
        self.wait_label = None
        banner = re.search(r"Flash writer for RZ/G3L[^\n]*", text).group(0).strip()
        self.expect([FW_PROMPT], 10)
        return banner

    def fw_cmd(self, line: str, expect: str, timeout: float = 10) -> str:
        self.buf = ""
        self.send(line + "\r")
        return self.expect([expect], timeout)[1]

    def fw_speed_up(self) -> None:
        self.fw_cmd("SUP", r"Please change to 921\.6Kbps")
        time.sleep(0.2)
        self.baud(921600)
        self.fw_cmd("", FW_PROMPT)

    def fw_em_w(self, area: int, sector: int, prog_start: int, srec: Path) -> None:
        self.fw_cmd("EM_W", r"Select area\(0-2\)>")
        self.fw_cmd(str(area), r"Please Input Start Address in sector :")
        self.fw_cmd(f"{sector:x}", r"Please Input Program Start Address :")
        self.fw_cmd(f"{prog_start:x}", r"please send !", 30)
        self.buf = ""
        self.send_file(srec)
        self.wait_label = "Flash Writer 正在写入 eMMC"
        self.expect([r"EM_W Complete!"], 300)
        self.wait_label = None
        self.expect([FW_PROMPT], 10)

    def fw_em_secsd(self, index: int, value: int) -> None:
        self.fw_cmd("em_secsd", r"Please Input EXT_CSD Index")
        self.fw_cmd(f"{index:x}", r"Please Input Value")
        out = self.fw_cmd(f"{value:x}", FW_PROMPT)
        if not re.search(rf"EXT_CSD\[{index:X}\] = 0x{value:02X}", out, re.I):
            raise BoardError(f"EXT_CSD[{index:#x}] not set to {value:#04x}: {out!r}")

    def fw_xls2(self, prog: int, save: int, srec: Path, size: int) -> dict:
        """QSPI write (SUG 4.4): program top address, QSPI save address, S-records; answer 'y' to
        "Clear OK?(y/n)". Returns the start/end address Flash Writer reports."""
        self.fw_cmd("XLS2", r"Program Top Address[\s\S]*?Please Input : H'")
        self.fw_cmd(f"{prog:x}", r"Qspi Save Address[\s\S]*?Please Input : H'")
        self.fw_cmd(f"{save:x}", r"please send !", 30)
        self.buf = ""
        self.send_file(srec)
        self.wait_label = "Flash Writer 正在擦除并写入 QSPI"
        text = ""
        while True:            # "Clear OK?(y/n)" may come before the erase or after the save information (SUG 4.4)
            i, part = self.expect([r"Clear OK\?\(y/n\)",
                                   # the board prints "Qspi  Save Information" (two spaces; 2026-11-26)
                                   r"Qspi\s+Save\s+Information[\s\S]*?End Address\s*:\s*H'[0-9A-Fa-f]+[\s\S]*?" + FW_PROMPT], 300)
            text += part
            if i == 1:
                break
            self.send("y")
            if re.search(r"Qspi\s+Save\s+Information", text):
                text += self.expect([FW_PROMPT], 60)[1]
                break
        self.wait_label = None
        st = re.search(r"Stat Address\s*:\s*H'([0-9A-Fa-f]+)", text)
        en = re.search(r"End Address\s*:\s*H'([0-9A-Fa-f]+)", text)
        got = {"start": int(st.group(1), 16) if st else None, "end": int(en.group(1), 16) if en else None}
        got["matches"] = got["start"] == save and got["end"] == save + size - 1
        return got

    def probe(self) -> str:
        """Where is the board? 'uboot', 'fw' (Flash Writer prompt; left at 921600 or 115200 accordingly) or
        'unknown' (off, BootROM, Linux, ...). Sends only Ctrl-C and CR; every caller asks for power-on/RESET
        when the answer is 'unknown', so a stray byte to the BootROM does no harm."""
        if self.at_prompt(1.0):
            return "uboot"
        for rate in (921600, 115200):
            self.baud(rate)
            self.buf = ""
            self.send("\r")
            try:
                self.expect([FW_PROMPT], 1.0)
                return "fw"
            except TimeoutError:
                pass
        self.baud(115200)
        return "unknown"

    # ---- U-Boot ----
    def at_prompt(self, wait: float = 1.0) -> bool:
        self.buf = ""
        self.send("\x03")
        try:
            self.expect([re.escape(PROMPT) + r"$"], wait)
            return True
        except TimeoutError:
            return False

    def stop_autoboot(self, wait: float, msg: str = "给 EVK 上电，或者按一下复位键。工具会自动打断 U-Boot 的自动启动。") -> None:
        """Get to the U-Boot prompt. If the board is not already there, a person powers it on or resets it;
        from the first boot output on, keep pressing a key until the prompt appears (also works with bootdelay=0)."""
        if self.at_prompt():
            return
        self.ask_human(msg)
        self.watch("等待板子启动到 U-Boot")
        deadline = time.time() + wait
        seen = bool(self.buf.strip())     # output already arrived during at_prompt(): the board is booting now
        while time.time() < deadline:
            if self._pump():
                seen = True
            if self.progress:
                self.progress.waiting(self.wait_label if not seen else "正在打断 U-Boot 的自动启动")
            if seen:
                self.send(" ")
                if re.search(re.escape(PROMPT) + r"\s*$", self.buf):
                    time.sleep(0.2)
                    if self.at_prompt():
                        return
            else:
                time.sleep(0.05)
        raise TimeoutError("没有等到 U-Boot 提示符。确认串口线（Carrier II 的 micro-USB 'UART Debug' 口）、"
                           "电源，以及 SW_MODE 设成了能启动到 U-Boot 的模式")

    def cmd(self, line: str, timeout: float = 10) -> str:
        """Run one U-Boot command; return its output (without the echoed command line and the prompt)."""
        self.buf = ""
        self.send(line + "\r")
        _, text = self.expect([r"\n" + re.escape(PROMPT) + r"$"], timeout)
        out = text.split("\n", 1)[1] if "\n" in text else ""
        return out[: -len(PROMPT)].rstrip("\n") if out.endswith(PROMPT) else out

    def mmc_select(self, dev: int, part: int | None = None) -> dict:
        out = self.cmd(f"mmc dev {dev}" + ("" if part is None else f" {part}"))
        if "is current device" not in out:
            raise BoardError(f"mmc dev {dev} failed: {out!r}")
        info = self.cmd("mmc info")
        m = re.search(r"^(SD|MMC) version", info, re.M)
        cap = re.search(r"^(?:User )?Capacity: ([\d.]+) ([KMGT])iB", info, re.M)
        if not m or not cap:
            raise BoardError(f"cannot read mmc info: {info!r}")
        size = float(cap.group(1)) * 1024 ** " KMGT".index(cap.group(2))
        return {"type": m.group(1), "capacity": int(size), "info": info}

    def net_setup(self) -> str:
        """Static addresses (no DHCP needed). Try eth0, then eth1 (the two RJ45 jacks of Carrier II).

        The EVK has no MAC address for U-Boot: without ethaddr both ports fail with "No valid MAC address found"
        and U-Boot prints "No ethernet found" (seen 2026-11-25). Locally administered MACs are set in RAM only
        (not saved; evk-write-emmc resets to the default environment before saveenv). A port without a cable
        costs a PHY auto-negotiation timeout before U-Boot moves on to the next port, hence the long ping wait."""
        if "not defined" in self.cmd("printenv ethaddr"):
            self.cmd(f"setenv ethaddr {LOCAL_MACS[0]}")
            self.cmd(f"setenv eth1addr {LOCAL_MACS[1]}")
        for k, v in (("ipaddr", BOARD_IP), ("serverip", HOST_IP), ("netmask", NETMASK)):
            self.cmd(f"setenv {k} {v}")
        for eth in ("eth0", "eth1"):
            self.cmd(f"setenv ethact {eth}")
            out = self.cmd(f"ping {HOST_IP}", timeout=60)
            if f"host {HOST_IP} is alive" in out:
                devs = re.findall(r"Using (\S+) device", out)
                if devs:
                    self.cmd(f"setenv ethact {devs[-1]}")   # keep the port that works; skip the dead one's timeout
                return f"{eth} ({devs[-1] if devs else '?'})"
        raise BoardError("ping 主机失败：确认 USB 网卡接在 Carrier II 的 RJ45 口上，并且主机端 evk-direct 已经连上"
                         "（nmcli con show evk-direct）")

    bytes_per_hash = 10 * 1468      # U-Boot prints '#' every 10 blocks; CONFIG_TFTP_BLOCKSIZE 1468; recalibrated

    def tftp(self, addr: int, path: str, size: int, base: int = 0, total: int | None = None) -> None:
        if self.progress:
            def tick():
                loading = self.buf.split("Loading:", 1)
                hashes = loading[1].count("#") if len(loading) == 2 else 0
                self.progress.update(base + min(size, hashes * self.bytes_per_hash), total or size)
            self.tick = tick
        try:
            out = self.cmd(f"tftpboot {addr:#x} {path}", timeout=max(60, size / 2e5))
        finally:
            self.tick = None
        m = re.search(r"Bytes transferred = (\d+)", out)
        if not m or int(m.group(1)) != size:
            raise BoardError(f"tftp {path}: {out[-300:]!r}")
        hashes = out.split("Loading:", 1)[1].count("#") if "Loading:" in out else 0
        if hashes >= 5:
            self.bytes_per_hash = size / hashes

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

    def write_chunks(self, man: dict, rel: str, verify: bool = True) -> None:
        """Per chunk: tftpboot -> crc32 must match -> mmc write (to the current mmc device); then read-back verify."""
        t0, done = time.time(), 0
        for i, c in enumerate(man["chunks"]):
            for attempt in range(3):
                try:
                    self.tftp(LOADADDR, f"{rel}/{c['file']}", c["bytes"], base=done, total=man["mapped_bytes"])
                    got = self.crc32(LOADADDR, c["bytes"])
                    if got != c["crc32"]:
                        raise BoardError(f"{c['file']}: crc32 {got:08x} != {c['crc32']:08x} after tftp")
                    break
                except (BoardError, TimeoutError) as e:
                    self.say(f"  {c['file']}: attempt {attempt + 1} failed: {e}")
                    if attempt == 2:
                        raise
            self.mmc_rw("write", LOADADDR, c["sector"], c["sectors"])
            done += c["bytes"]
            rate = done / max(time.time() - t0, 1e-3)
            line = (f"  [{i + 1}/{len(man['chunks'])}] {c['file']} -> sector {c['sector']:#x}  "
                    f"{done / 2**20:.0f}/{man['mapped_bytes'] / 2**20:.0f} MiB  {rate / 2**20:.1f} MiB/s")
            if self.progress:
                self.progress.update(done, man["mapped_bytes"])
                if not self.progress.tty:
                    self.say(line)
            else:
                self.say(line)
        if self.progress:
            secs = max(time.time() - t0, 1e-3)
            self.progress.finish(f"{man['mapped_bytes'] / 2**20:.0f} MiB，平均 {man['mapped_bytes'] / 2**20 / secs:.1f} MiB/s")
        else:
            self.say(f"write: done in {time.time() - t0:.0f}s")
        if verify:
            t1 = time.time()
            if self.progress:
                self.progress.start("verify")
            for n, c in enumerate(man["chunks"], 1):
                self.mmc_rw("read", LOADADDR, c["sector"], c["sectors"])
                got = self.crc32(LOADADDR, c["bytes"])
                if got != c["crc32"]:
                    raise BoardError(f"read-back of {c['file']} (sector {c['sector']:#x}): "
                                     f"crc32 {got:08x} != {c['crc32']:08x}")
                if self.progress:
                    self.progress.update(n, len(man["chunks"]), unit="块")
            msg = f"verify: all {len(man['chunks'])} chunks read back with matching CRC32 ({time.time() - t1:.0f}s)"
            if self.progress:
                self.progress.finish(msg)
            else:
                self.say(msg)

    # ---- Linux on the board (serial console; the images have debug-tweaks: root without password) ----
    def linux_shell(self, wait: float) -> None:
        """Get to a root shell: from the shell, the login prompt, U-Boot (boot) or a powered-off board."""
        self.buf = ""
        self.send("\r")
        try:
            i, _ = self.expect([SHELL_PROMPT, r"login: $", re.escape(PROMPT) + r"$"], 3)
        except TimeoutError:
            self.ask_human("给 EVK 上电（eMMC 启动：SW_MODE[1:3] = ON OFF OFF），工具会等到 Linux 的登录提示。")
            self.watch("等待 Linux 的登录提示")
            i, _ = self.expect([SHELL_PROMPT, r"login: $"], wait)
            self.wait_label = None
        if i == 2:                                   # U-Boot prompt: boot with the default command
            self.send("boot\r")
            i, _ = self.expect([SHELL_PROMPT, r"login: $"], wait)
        if i == 1:
            self.login()

    def login(self) -> None:
        self.buf = ""
        self.send("root\r")
        i, _ = self.expect([SHELL_PROMPT, r"Password: $"], 30)
        if i == 1:
            raise BoardError("the image asks for a password: evk-verify needs an image with debug-tweaks")

    def linux_cmd(self, cmd: str, timeout: float = 30) -> tuple[int, str]:
        """Run a shell command; return (exit status, output). The end is found by a marker with the status.

        The marker is printed on a line of its own (printf starts it with a newline), so output that does not end
        in a newline (busybox `hexdump -e`, `printf`) still ends the command; the extra blank line is stripped."""
        mark = f"__RZ{int(time.time() * 1000) % 100000:05d}__"
        self.buf = ""
        self.send(f"{cmd}; printf '\\n%s%d\\n' {mark} $?\r")
        _, text = self.expect([rf"\n{mark}(\d+)\s*\n[\s\S]*?{SHELL_PROMPT}"], timeout)
        m = re.search(rf"\n{mark}(\d+)", text)
        body = text[:m.start()]
        body = body.split("\n", 1)[1] if "\n" in body else ""      # drop the echoed command line
        return int(m.group(1)), body.strip("\n")

    def reboot_timed(self, timeout: float) -> dict:
        """reboot from the shell; seconds until the U-Boot banner, 'Starting kernel' and the login prompt."""
        self.buf = ""
        t0 = time.time()
        self.watch("重启中")
        self.send("reboot\r")
        times = {}
        for key, rx in (("u_boot", r"U-Boot 20\d\d"), ("kernel", r"Starting kernel"), ("login", r"login: $")):
            self.expect([rx], timeout)
            times[key] = round(time.time() - t0, 1)
        self.wait_label = None
        return times
