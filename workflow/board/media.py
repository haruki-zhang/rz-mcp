"""Boot media layouts of the RZ/G3L SMARC EVK, in one place. Every value names its source.

Sources:
  SUG  Linux Start-up Guide RZ/G3L, R01US0858EJ0100 Rev.1.00-rc1 (Jan. 16, 2026)
  LPD  RZ/G3L LowPower Display guide (rzg3l-lowpower-display/docs), section 4.1
  EBM  RZ/G3L SMARC Module Board EVK manual R01UH1193EJ0100 Rev.1.00; Carrier II manual R01UH1077EJ0120 Rev.1.20
  EVK  observed on the board, 2026-11-25 (data/board-runs/)
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BootFile:
    role: str            # bl2 | fip | cm33
    srec: str            # file in the deploy directory; "{cm33}" is resolved at run time
    prog: int            # program (RAM load) address given to Flash Writer
    dest: int            # eMMC: start sector in boot partition 1. QSPI: save address in bytes.
    wic_sector: int | None   # where the eSD WIC of the same build embeds the same bytes (cross-check)
    source: str


FLASH_WRITER = "Flash_Writer_SCIF_RZG3L_SMARC_LPDDR4.mot"
CM33_GLOB = "*-cm33-fw-smarc-rzg3l.srec"

EMMC = [
    BootFile("bl2", "bl2_bp_mmc-smarc-rzg3l.srec", 0x21E00, 0x1, None, "SUG 8.2.1 Table 14 (named bl2_bp_emmc there)"),
    BootFile("fip", "fip-smarc-rzg3l.srec", 0x0, 0x300, 768, "SUG 8.2.1 Table 14"),
    BootFile("cm33", "{cm33}", 0x0, 0x1380, 4992, "LPD 4.1; EVK: U-Boot reports 'CM33 state 1' after eMMC boot"),
]
EMMC_PARTITION = 1
EMMC_BOOT_PART_SECTORS = 0xFC00           # Flash Writer: "1:Boot Partition 1 : 32256 KBytes" (SUG 8.2.1, EVK)
EMMC_EXT_CSD = [(0xB1, 0x02), (0xB3, 0x08)]   # SUG 8.2.1 Table 13

QSPI = [
    BootFile("bl2", "bl2_bp_spi-smarc-rzg3l.srec", 0x21E00, 0x0, None, "SUG 4.4 Table 11"),
    BootFile("fip", "fip-smarc-rzg3l.srec", 0x0, 0x60000, 768, "SUG 4.4 Table 11"),
    BootFile("cm33", "{cm33}", 0x0, 0x270000, 4992, "LPD 4.1 (not yet verified on the EVK)"),
]
QSPI_SIZE = 16 << 20                      # AT25QL128A, 128 Mbit (EBM R01UH1193EJ0100 3.6)

# SW_MODE[1:3] on Carrier II (SUG Table 4; Carrier II EBM Table 2.12). Module BOOT/SYS: all OFF (factory default,
# EBM R01UH1193EJ0100 2.4.1/2.4.2): SYS[1] OFF = SD0 on eMMC, BOOT[2] OFF = CA55 cold boot.
SW_MODE = {"scif": "OFF ON OFF", "emmc": "ON OFF OFF", "qspi": "OFF OFF OFF", "esd": "ON ON OFF"}

MSG_SCIF = (f"设成 SCIF 下载模式：SW_MODE[1:3] = {SW_MODE['scif']}。模块上的 BOOT 和 SYS 开关全部为出厂默认 OFF"
            "（BOOT[2] = OFF，即 CA55 冷启动）。然后按 POWER 上电；如果板子已经通电，就按 RESET。")
MSG_EMMC = (f"设成 eMMC 启动：SW_MODE[1:3] = {SW_MODE['emmc']}；模块 SYS[1] = OFF（SD0 接 eMMC）；"
            "Carrier 上的 microSD1 槽不要插卡（否则 U-Boot 会优先从那里启动）。然后断电再上电。")
MSG_QSPI = (f"设成 QSPI 启动：SW_MODE[1:3] = {SW_MODE['qspi']}。rootfs 由 U-Boot 从 mmc 0:2 读取："
            "SYS[1] = OFF 时读 eMMC，SYS[1] = ON 时读模块上的 microSD0。然后断电再上电。")
