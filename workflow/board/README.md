# workflow/board/：上板（第 6 步，B3）

把 WIC 镜像写进 SMARC 模块上的 microSD，**不需要拔插卡**：板子先进入 U-Boot，再用 TFTP 分块下载，用 `mmc write` 写入，最后读回校验。上电、复位和拨码由人完成，其余步骤都由工具自动执行。方案见 `plan/PoC_Workflow_Plan.md` 7.3 节。

| 文件 | 作用 |
|---|---|
| `setup-host.sh` | 主机配置，**用 sudo 运行一次**：串口的 udev 规则，USB 网卡的固定 IP，dnsmasq（只在 USB 网卡上提供 DHCP 和 TFTP），`/srv/tftp`。加 `--undo` 可以撤销 |
| `evk.py` | 串口控制台（带日志）和 U-Boot 操作。输出格式按 BSP 所用 rev 的 U-Boot 源码核对过 |
| `evk-write-sd` | 写 microSD：准备分块 → 进入 U-Boot → 逐块 TFTP、CRC32、`mmc write` → 读回校验 → 启动确认 |
| `evk-write-emmc` | **eMMC 启动**，适用于还没有 U-Boot 的板子：先用 Flash Writer 把 BL2、FIP、CM33 固件写进 eMMC boot 分区 1，并设置 EXT_CSD；再从 eMMC 启动 U-Boot，把 rootfs 写进 user 区；最后 `saveenv` 并重启，确认启动成功 |

## 一次性准备（主机）

```sh
sudo bash workflow/board/setup-host.sh
```

## 每次写板子之前（人）

1. USB 网卡接到 Carrier II 上的任意一个 RJ45 口。工具会先试 eth0，不通再试 eth1。
2. Carrier II 上标着 "UART Debug" 的 micro-USB 口（SER3，FT234XD）接到主机。接上后会出现 `/dev/rzg3l-evk-console`。

拨码开关怎么设，见下面各流程的说明。

## eMMC 启动（板子上没有 U-Boot 也可以）

```sh
workflow/board/evk-write-emmc --image <deploy>/<x>.wic.gz    # 启动文件从同一个 deploy 目录读取
```

| 阶段 | 人做什么 | 工具做什么 |
|---|---|---|
| 1 | SW_MODE[1:3] = OFF ON OFF（SCIF 下载模式）。模块上的 BOOT 和 SYS 全部为出厂默认 OFF：SYS[1] = OFF 表示 SD0 接 eMMC，BOOT[2] = OFF 表示 CA55 冷启动（模块手册 R01UH1193EJ0100 §2.4.1、§2.4.2）。然后上电 | 以 115200 发送 Flash Writer，`SUP` 切到 921600；`EM_W` 写入：bl2_bp_mmc → 分区 1 sector 0x1（加载地址 0x21e00），fip → 0x300（0x0），lpdemo-cm33-fw → 0x1380（0x0）；`em_secsd` 设 B1=0x02、B3=0x08 |
| 2 | SW_MODE[1:3] = ON OFF OFF（eMMC），Carrier 上的 microSD1 槽不插卡，断电后重新上电 | U-Boot 进入 `mmc dev 0 0`（必须是 eMMC），通过 TFTP 写入 user 区，读回校验，`env default -a; saveenv`，执行 `reset`，等待 `login:` |

地址的出处：Start-up Guide R01US0858EJ0100 §8.2.1 表 13 和表 14；CM33 固件的地址来自 LowPower 指南 §4.1，该节注明为 "Not Verified"。
碰板子之前，工具先做以下检查：每个 .srec 解码后要和同一次构建的 .bin 一致，起始地址要等于加载地址，并且不能和下一个文件重叠。

## 写入（microSD）

写之前：模块上的 SYS[1] 打到 ON，选择 microSD0（工具只写 SD 卡，检测到 eMMC 会拒绝写入）；SW_MODE 设成板子上**现有**镜像能启动到 U-Boot 的模式，例如 eSD：SW_MODE[1:3] = ON ON OFF。

```sh
workflow/board/evk-write-sd --image <x.wic.gz>      # 同一目录下要有 x.wic.bmap
```

工具运行到需要人操作的地方，会打印 `>>> 需要你操作：…`，然后通过串口判断操作是否已经完成，不需要在键盘上应答：

1. 给板子上电，或者按复位键。工具会自动打断 U-Boot 的自动启动。
2. 写入和校验完成后，确认 SW_MODE 是 eSD，然后断电再上电。工具会等串口上出现 `--expect` 指定的文字（默认是 `login:`）。如果镜像屏蔽了串口登录，就换一个能在启动日志里看到的文字。

串口日志保存在 `data/board-runs/` 下。离线测试：`/usr/bin/python3 eval/test_board.py [--real-image x.wic.gz]`。测试使用 `eval/fake_uboot.py`，不需要板子，也不需要 sudo。

## 还没有覆盖的情况

- 板子上的现有镜像起不到 U-Boot 时，要先用 Flash Writer 经 SCIF 下载模式写入 bootloader。这一步还没有自动化。
- NFS root 和 Linux 里的测试执行，属于 B3 的后续部分。
