# Run: /usr/bin/python3 eval/test_board.py [--real-image <x.wic.gz>]   (system python3: needs pyserial)
"""Offline tests for workflow/board/evk-flash against eval/fake_uboot.py (no board, no sudo).

An operator thread plays the person: when the tool prints ">>> 需要你操作", it powers the fake board on in the
boot mode the request names (SCIF / eMMC / QSPI). Known answers: what lands in the fake eMMC boot partition and
QSPI must equal the build's .bin files at the addresses in media.py; the eMMC user area must match every sha256
in the bmap (the check bmaptool uses); EXT_CSD must be B1=0x02, B3=0x08.
"""
import argparse
import gzip
import hashlib
import json
import random
import shutil
import subprocess
import sys
import threading
from pathlib import Path

RZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RZ / "eval"))
sys.path.insert(0, str(RZ / "workflow" / "board"))
from fake_uboot import FakeUBoot  # noqa: E402
import evk    # noqa: E402
import media  # noqa: E402

TOOL = RZ / "workflow" / "board" / "evk-flash"
DEPLOY = Path("/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1/build/tmp/deploy/images/smarc-rzg3l")
WORK = Path("/tmp/board-tests")
TFTP = WORK / "tftp"
CM33 = "lpdemo-cm33-fw-smarc-rzg3l"
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def synthetic_image(path: Path, fip: bytes, cm33: bytes, bs=4096, size=8 << 20) -> Path:
    """A small eSD-style WIC: fip at 768s and the CM33 firmware at 4992s (as lpdemo-bootpart-esd.wks places them),
    plus random 'filesystem' data; with its bmap."""
    rnd = random.Random(7)
    img = bytearray(size)
    img[0:4096] = rnd.randbytes(4096)
    img[768 * 512:768 * 512 + len(fip)] = fip
    img[4992 * 512:4992 * 512 + len(cm33)] = cm33
    img[0x400000:0x500000] = rnd.randbytes(0x100000)
    spans = [(0, 4096), (768 * 512, 768 * 512 + len(fip)), (4992 * 512, 4992 * 512 + len(cm33)), (0x400000, 0x500000)]
    ranges = [(a // bs, (b - 1) // bs) for a, b in spans]
    with gzip.open(path, "wb") as f:
        f.write(img)
    rows = "".join(f'        <Range chksum="{hashlib.sha256(img[a * bs:(b + 1) * bs]).hexdigest()}"> {a}-{b} </Range>\n'
                   for a, b in ranges)
    Path(str(path).replace(".wic.gz", ".wic.bmap")).write_text(
        f'<?xml version="1.0" ?>\n<bmap version="2.0">\n    <ImageSize> {size} </ImageSize>\n'
        f"    <BlockSize> {bs} </BlockSize>\n    <BlocksCount> {size // bs} </BlocksCount>\n"
        f"    <MappedBlocksCount> {sum(b - a + 1 for a, b in ranges)} </MappedBlocksCount>\n"
        f"    <ChecksumType> sha256 </ChecksumType>\n    <BlockMap>\n{rows}    </BlockMap>\n</bmap>\n")
    return path


def user_area_matches(sd: Path, image: Path) -> bool:
    bm = evk.read_bmap(Path(str(image).replace(".wic.gz", ".wic.bmap")))
    with open(sd, "rb") as f:
        for a, b, chk in bm["ranges"]:
            f.seek(a * bm["block_size"])
            if hashlib.sha256(f.read(min((b + 1) * bm["block_size"], bm["image_size"]) - a * bm["block_size"])).hexdigest() != chk:
                return False
    return True


def content_ok(store: Path, table: list, unit: int, cm33: str | None = CM33) -> bool:
    with open(store, "rb") as f:
        for bf in table:
            name = cm33 if bf.role == "cm33" else bf.srec.removesuffix(".srec")
            if name is None:
                continue
            want = (DEPLOY / f"{name}.bin").read_bytes()
            f.seek(bf.dest * unit)
            if f.read(len(want)) != want:
                print(f"   {store.name}: {name} differs at {bf.dest * unit:#x}")
                return False
    return True


def flash(args: list, fake: FakeUBoot | None, port: str | None = None) -> tuple[int, str]:
    cmd = [sys.executable, "-u", str(TOOL), *args, "--tftp-root", str(TFTP), "--log-dir", str(WORK / "logs"),
           "--wait", "30", "--skip-host-checks"]
    if fake or port:
        cmd += ["--port", port or fake.port]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = []
    for line in proc.stdout:
        out.append(line)
        if fake and "需要你操作" in line:
            mode = "scif" if "SCIF" in line else "emmc" if "eMMC 启动" in line else "qspi" if "QSPI 启动" in line else None
            if mode:
                threading.Timer(0.5, fake.power_on, args=(mode,)).start()
    proc.wait(timeout=1800)
    if fake:
        fake.stop = True
    return proc.returncode, "".join(out)


def board(name: str, **kw) -> FakeUBoot:
    return FakeUBoot(TFTP, WORK / f"emmc-{name}.img", boot1=WORK / f"boot1-{name}.img", qspi=WORK / f"qspi-{name}.img", **kw)


def last_report() -> dict:
    return json.loads(sorted((WORK / "logs").glob("*/report.json"), key=lambda p: p.stat().st_mtime)[-1].read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-image", type=Path, help="also run the whole eMMC flow with a real .wic.gz")
    a = ap.parse_args()
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    fip, cm33 = (DEPLOY / "fip-smarc-rzg3l.bin").read_bytes(), (DEPLOY / f"{CM33}.bin").read_bytes()
    img = synthetic_image(WORK / "syn.rootfs.wic.gz", fip, cm33)
    other = synthetic_image(WORK / "other-build.rootfs.wic.gz", fip[:-1] + bytes([fip[-1] ^ 1]), cm33)
    deploy = ["--deploy", str(DEPLOY)]

    # ---- doctor (writes nothing)
    f = board("doctor")
    rc, out = flash(["doctor", "--image", str(img), *deploy], None, port=f.port)
    f.stop = True
    check(rc == 0 and f"cm33 from image: {CM33}.srec" in out and "= image" in out, "doctor: files consistent with the image, CM33 picked from it")

    # ---- scenario 1 + 2: eMMC on a board without U-Boot
    f = board("emmc")
    rc, out = flash(["emmc", "--image", str(img), *deploy], f)
    check(rc == 0 and out.count("需要你操作") == 2, f"emmc: two manual steps, OK ({out.strip()[-200:] if rc else ''})")
    check(content_ok(f.boot1, media.EMMC, 512), "emmc: boot partition 1 = bl2 @0x1, fip @0x300, cm33 @0x1380 (.bin files)")
    check(f.ext_csd[0xB1] == 0x02 and f.ext_csd[0xB3] == 0x08, "emmc: EXT_CSD B1=0x02, B3=0x08")
    check(user_area_matches(f.sd, img), "emmc: user area matches every bmap sha256")
    check("MAC" not in out and "network: eth0 (ethernet@11c40000)" in out, "emmc: no MAC on the board handled; port that answers is kept")
    rep = last_report()
    check(rep["result"] == "OK" and len(rep["written"]) == 4 and rep["versions"].get("flash_writer", "").startswith("Flash writer")
          and "u_boot" in rep["versions"], "emmc: report.json has result, 4 written items, Flash Writer and U-Boot versions")

    # ---- scenario 1: QSPI
    f = board("qspi")
    rc, out = flash(["bootloader", "qspi", "--image", str(img), "--boot-check", "uboot", *deploy], f)
    check(rc == 0 and out.count("XLS2 ") == 3, f"qspi: XLS2 x3 incl. 'Clear OK?(y/n)' ({out.strip()[-200:] if rc else ''})")
    check(content_ok(f.qspi, media.QSPI, 1), "qspi: bl2_bp_spi @0x0, fip @0x60000, cm33 @0x270000 (.bin files)")
    check("warning: Flash Writer reported" not in out, "qspi: reported start/end addresses match")
    check("boot check: 'U-Boot 20" in out, "qspi: boot check after switching to QSPI boot")

    # ---- scenario 1: eMMC bootloader only, no image (CM33: the only candidate)
    f = board("bl-emmc")
    rc, out = flash(["bootloader", "emmc", *deploy], f)
    check(rc == 0 and "only candidate" in out and content_ok(f.boot1, media.EMMC, 512), "bootloader emmc without --image")

    # ---- resume: Flash Writer already running (e.g. after an interrupted run)
    f = board("resume", state="fw")
    f.fw = {"step": "cmd", "lines": []}
    rc, out = flash(["bootloader", "emmc", "--image", str(img), *deploy], f)
    check(rc == 0 and "already running; skipping the download" in out and "需要你操作" not in out
          and content_ok(f.boot1, media.EMMC, 512), "resume at the Flash Writer prompt: no download, no manual step")

    # ---- scenario 2 alone: board already at the U-Boot prompt
    f = board("rootfs", state="prompt")
    rc, out = flash(["rootfs", "--image", str(img)], f)
    check(rc == 0 and "需要你操作" not in out and user_area_matches(f.sd, img) and "boot check: 'login:'" in out,
          "rootfs from the U-Boot prompt: written, verified, saveenv, reset, login")

    # ---- safety
    f = board("on-sd", state="prompt", mmc_type="SD")
    rc, out = flash(["rootfs", "--image", str(img)], f)
    check(rc != 0 and "not the eMMC" in out and f.writes == 0 and last_report()["result"] == "FAILED",
          "rootfs refuses an SD card on mmc 0; nothing written; report says FAILED")

    f = board("other", state="prompt")
    rc, out = flash(["emmc", "--image", str(other), *deploy], f)
    check(rc != 0 and "different builds" in out and f.writes == 0, "image from another build is refused before touching the board")

    alt = WORK / "deploy-mismatch"
    alt.mkdir()
    for p in DEPLOY.iterdir():
        if p.name.startswith(("Flash_Writer", "bl2_bp_", "fip-smarc-rzg3l.", CM33)) and p.is_file():
            shutil.copy(p.resolve(), alt / p.name)
    (alt / "fip-smarc-rzg3l.bin").write_bytes(b"other build")
    rc, out = flash(["doctor", "--deploy", str(alt)], None, port=board("mm").port)
    check(rc != 0 and "does not match fip-smarc-rzg3l.bin" in out, ".srec and .bin from different builds are refused")

    f = board("corrupt", state="prompt", corrupt_first_tftp=True)
    rc, out = flash(["rootfs", "--image", str(img)], f)
    check(rc == 0 and "attempt 1 failed" in out and user_area_matches(f.sd, img), "a corrupted TFTP transfer is caught by CRC32 and retried")

    f = board("lock", state="prompt")
    holder = evk.Console(f.port)
    rc, out = flash(["rootfs", "--image", str(img)], f)
    holder.close()
    check(rc != 0 and "另一个工具或终端程序正在使用串口" in out and f.writes == 0, "a console already in use is refused")

    bad = WORK / "bad.rootfs.wic.gz"
    shutil.copy(img, bad)
    Path(str(bad).replace(".wic.gz", ".wic.bmap")).write_text(
        '<?xml version="1.0"?><!DOCTYPE b [<!ENTITY x "y">]><bmap version="2.0"/>')
    rc, out = flash(["rootfs", "--image", str(bad)], None, port=board("bad").port)
    check(rc != 0 and "refusing a bmap with a DOCTYPE" in out, "bmap with DOCTYPE/ENTITY is refused")

    rc, out = flash(["clean", "--keep", "0"], None, port=board("clean").port)
    check(rc == 0 and not list((TFTP / "evk").glob("*/manifest.json")), "clean removes staged images")

    if a.real_image:
        f = board("real")
        rc, out = flash(["emmc", "--image", str(a.real_image.resolve())], f)
        print("".join(l for l in out.splitlines(True) if l.startswith(("check:", "XLS2", "EM_W", "boot check", "verify"))))
        check(rc == 0 and content_ok(f.boot1, media.EMMC, 512) and user_area_matches(f.sd, a.real_image.resolve()),
              "real image, whole eMMC flow: boot partition 1 = .bin files, user area = bmap")

    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
