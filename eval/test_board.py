# Run: /usr/bin/python3 eval/test_board.py [--real-image <x.wic.gz>]   (system python3: needs pyserial)
"""Offline tests for workflow/board/evk-write-sd against eval/fake_uboot.py (no board, no sudo).

Known answer: after writing, every bmap range read back from the fake SD card must have the sha256 that
Yocto recorded in the .wic.bmap; that is the same check bmaptool uses.
"""
import argparse
import gzip
import hashlib
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RZ / "eval"))
from fake_uboot import FakeUBoot  # noqa: E402

TOOL = RZ / "workflow" / "board" / "evk-write-sd"
WORK = Path("/tmp/board-tests")
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def synthetic_image(path: Path, size=8 << 20, bs=4096) -> Path:
    """A small WIC-like image with 4 mapped ranges and its bmap."""
    rnd = random.Random(7)
    img = bytearray(size)
    ranges = [(0, 1), (10, 10), (300, 811), (1900, 2047)]
    for a, b in ranges:
        img[a * bs:(b + 1) * bs] = rnd.randbytes((b - a + 1) * bs)
    with gzip.open(path, "wb") as f:
        f.write(img)
    rows = "".join(f'        <Range chksum="{hashlib.sha256(img[a * bs:(b + 1) * bs]).hexdigest()}"> '
                   f'{a}-{b} </Range>\n' for a, b in ranges)
    mapped = sum(b - a + 1 for a, b in ranges)
    Path(str(path).replace(".wic.gz", ".wic.bmap")).write_text(
        f'<?xml version="1.0" ?>\n<bmap version="2.0">\n    <ImageSize> {size} </ImageSize>\n'
        f"    <BlockSize> {bs} </BlockSize>\n    <BlocksCount> {size // bs} </BlocksCount>\n"
        f"    <MappedBlocksCount> {mapped} </MappedBlocksCount>\n    <ChecksumType> sha256 </ChecksumType>\n"
        f"    <BlockMap>\n{rows}    </BlockMap>\n</bmap>\n")
    return path


def sd_matches_bmap(sd: Path, bmap: Path) -> bool:
    ews = {"__file__": str(TOOL), "__name__": "evk_write_sd"}      # the tool's own bmap parser
    exec(compile(TOOL.read_text(), str(TOOL), "exec"), ews)
    bm = ews["read_bmap"](bmap)
    with open(sd, "rb") as f:
        for a, b, chk in bm["ranges"]:
            f.seek(a * bm["block_size"])
            data = f.read(min((b + 1) * bm["block_size"], bm["image_size"]) - a * bm["block_size"])
            if hashlib.sha256(data).hexdigest() != chk:
                return False
    return True


def run_tool(image: Path, fake: FakeUBoot | None, tftp: Path, *extra) -> subprocess.CompletedProcess:
    args = [sys.executable, str(TOOL), "--image", str(image), "--tftp-root", str(tftp),
            "--log-dir", str(WORK / "logs"), "--wait", "20", *extra]
    if fake:
        args += ["--port", fake.port]
    return subprocess.run(args, capture_output=True, text=True, timeout=1800)


def case(name, image, tftp, extra=(), **fake_kw):
    sd = WORK / f"sd-{name}.img"
    fake = FakeUBoot(tftp, sd, 2 << 30, **fake_kw)
    r = run_tool(image, fake, tftp, *extra)
    fake.stop = True
    return r, fake, sd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-image", type=Path, help="also run the full write with a real .wic.gz (e.g. lpdemo)")
    a = ap.parse_args()
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    tftp = WORK / "tftp"
    img = synthetic_image(WORK / "syn-image.rootfs.wic.gz")
    bmap = Path(str(img).replace(".wic.gz", ".wic.bmap"))

    r = run_tool(img, None, tftp, "--prepare-only", "--chunk-mb", "1")
    check(r.returncode == 0 and "sha256 of all 4 bmap ranges OK" in r.stdout, f"prepare: synthetic image ({r.stdout.strip()[-120:]}{r.stderr[-200:]})")

    r, fake, sd = case("basic", img, tftp, ("--no-boot-check", "--chunk-mb", "1"))
    check(r.returncode == 0 and "需要你操作" in r.stdout, f"write via fake U-Boot after a (simulated) power-on ({r.stderr[-300:]})")
    check(sd_matches_bmap(sd, bmap), "fake SD matches every bmap sha256 (known answer)")
    check("verify: all" in r.stdout, "read-back verify ran")

    r, fake, sd = case("eth1", img, tftp, ("--no-boot-check",), eth_ok="eth1")
    check(r.returncode == 0 and "network: eth1" in r.stdout, "falls back to eth1 when eth0 has no link")

    r, fake, sd = case("emmc", img, tftp, ("--no-boot-check",), mmc_type="MMC")
    check(r.returncode != 0 and "refusing" in (r.stdout + r.stderr) and fake.writes == 0, "refuses eMMC; nothing written")

    r, fake, sd = case("corrupt", img, tftp, ("--no-boot-check",), corrupt_first_tftp=True)
    check(r.returncode == 0 and "attempt 1 failed" in r.stdout and sd_matches_bmap(sd, bmap),
          "a corrupted TFTP transfer is caught by CRC32 and retried")

    r, fake, sd = case("prompt", img, tftp, ("--no-boot-check",), at_prompt=True)
    check(r.returncode == 0 and "需要你操作" not in r.stdout, "board already at the prompt: no power-on request")

    r, fake, sd = case("boot", img, tftp, (), boot_after_verify=True)
    check(r.returncode == 0 and "boot check: 'login:' seen" in r.stdout, "boot check waits for 'login:'")

    bad = WORK / "bad-image.rootfs.wic.gz"
    shutil.copy(img, bad)
    Path(str(bad).replace(".wic.gz", ".wic.bmap")).write_text(
        '<?xml version="1.0"?><!DOCTYPE b [<!ENTITY x "y">]><bmap version="2.0"/>')
    r = run_tool(bad, None, tftp, "--prepare-only")
    check(r.returncode != 0 and "refusing a bmap with a DOCTYPE" in r.stderr, "bmap with DOCTYPE/ENTITY is refused")

    if a.real_image:
        real = a.real_image.resolve()
        r, fake, sd = case("real", real, tftp, ("--no-boot-check",))
        print(r.stdout[-900:])
        check(r.returncode == 0, f"real image written through fake U-Boot ({r.stderr[-300:]})")
        check(sd_matches_bmap(sd, Path(str(real).replace(".wic.gz", ".wic.bmap"))),
              "real image: fake SD matches every bmap sha256 (known answer)")

    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
