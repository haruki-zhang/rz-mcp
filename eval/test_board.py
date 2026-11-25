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
sys.path.insert(0, str(RZ / "workflow" / "board"))
import evk  # noqa: E402

TOOL = RZ / "workflow" / "board" / "evk-write-sd"
EMMC_TOOL = RZ / "workflow" / "board" / "evk-write-emmc"
DEPLOY = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1/build/tmp/deploy/images/smarc-rzg3l")
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
    bm = evk.read_bmap(bmap)                                      # the tools' own bmap parser
    with open(sd, "rb") as f:
        for a, b, chk in bm["ranges"]:
            f.seek(a * bm["block_size"])
            data = f.read(min((b + 1) * bm["block_size"], bm["image_size"]) - a * bm["block_size"])
            if hashlib.sha256(data).hexdigest() != chk:
                return False
    return True


def run_tool(image: Path, fake: FakeUBoot | None, tftp: Path, *extra, tool: Path = TOOL) -> subprocess.CompletedProcess:
    args = [sys.executable, str(tool), "--image", str(image), "--tftp-root", str(tftp),
            "--log-dir", str(WORK / "logs"), "--wait", "20", *extra]
    if fake:
        args += ["--port", fake.port]
    return subprocess.run(args, capture_output=True, text=True, timeout=1800)


def case(name, image, tftp, extra=(), tool=TOOL, **fake_kw):
    sd = WORK / f"sd-{name}.img"
    fake = FakeUBoot(tftp, sd, 2 << 30, **fake_kw)
    r = run_tool(image, fake, tftp, *extra, tool=tool)
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


    # ---- eMMC boot: Flash Writer (BL2, FIP, CM33) + rootfs from U-Boot
    def boot1_ok(boot1: Path) -> bool:
        with open(boot1, "rb") as f:
            for name, sector in (("bl2_bp_mmc-smarc-rzg3l", 0x1), ("fip-smarc-rzg3l", 0x300), ("lpdemo-cm33-fw-smarc-rzg3l", 0x1380)):
                want = (DEPLOY / f"{name}.bin").read_bytes()
                f.seek(sector * 512)
                if f.read(len(want)) != want:
                    print(f"   boot partition 1 at sector {sector:#x} differs from {name}.bin")
                    return False
        return True

    def emmc_case(name, image, extra=(), **kw):
        b1 = WORK / f"boot1-{name}.img"
        r, fake, sd = case(name, image, tftp, ("--deploy", str(DEPLOY), *extra), tool=EMMC_TOOL, boot1=b1, **kw)
        return r, fake, sd, b1

    r, fake, sd, b1 = emmc_case("emmc-full", img, scif=True)
    check(r.returncode == 0 and r.stdout.count("需要你操作") == 2, f"eMMC: board without U-Boot, two manual steps ({r.stderr[-300:]})")
    check(boot1_ok(b1), "eMMC boot partition 1: bl2 @0x1, fip @0x300, cm33 @0x1380 equal the build's .bin files")
    check(fake.ext_csd.get(0xB1) == 0x02 and fake.ext_csd.get(0xB3) == 0x08, "EXT_CSD B1=0x02, B3=0x08 (Start-up Guide Table 13)")
    check(sd_matches_bmap(sd, bmap), "eMMC user area matches every bmap sha256")
    check("env: default environment saved" in r.stdout and "boot check: 'login:' seen" in r.stdout, "saveenv, then reset boots to login")

    r, fake, sd, b1 = emmc_case("emmc-on-sd", img, ("--skip-bootloader",), mmc_type="SD")
    check(r.returncode != 0 and "not the eMMC" in (r.stdout + r.stderr) and fake.writes == 0, "eMMC tool refuses an SD card (SYS[1] = ON); nothing written")

    alt = WORK / "deploy-mismatch"
    alt.mkdir()
    for f in DEPLOY.glob("*"):
        if f.name.startswith(("Flash_Writer", "bl2_bp_mmc", "fip-smarc-rzg3l.", "lpdemo-cm33-fw-smarc-rzg3l.")) and f.is_file():
            shutil.copy(f.resolve(), alt / f.name)
    (alt / "fip-smarc-rzg3l.bin").write_bytes(b"other build")
    r = run_tool(img, None, tftp, "--deploy", str(alt), "--prepare-only", tool=EMMC_TOOL)
    check(r.returncode != 0 and "does not match fip-smarc-rzg3l.bin" in r.stderr, "boot files from different builds are refused before touching the board")

    if a.real_image:
        real = a.real_image.resolve()
        r, fake, sd = case("real", real, tftp, ("--no-boot-check",))
        print(r.stdout[-900:])
        check(r.returncode == 0, f"real image written through fake U-Boot ({r.stderr[-300:]})")
        check(sd_matches_bmap(sd, Path(str(real).replace(".wic.gz", ".wic.bmap"))),
              "real image: fake SD matches every bmap sha256 (known answer)")
        r, fake, sd, b1 = emmc_case("real-emmc", real, scif=True)
        print(r.stdout[-700:])
        check(r.returncode == 0 and boot1_ok(b1) and sd_matches_bmap(sd, Path(str(real).replace(".wic.gz", ".wic.bmap"))),
              "real image, eMMC flow: boot partition 1 equals the .bin files and the user area matches the bmap")

    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
