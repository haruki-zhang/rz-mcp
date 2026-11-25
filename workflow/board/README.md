# workflow/board/：上板（第 6 步，B3）

把 WIC 镜像写进 SMARC 模块上的 microSD，**不需要拔插卡**：板子先进入 U-Boot，再用 TFTP 分块下载，用 `mmc write` 写入，最后读回校验。上电、复位和拨码由人完成，其余步骤都由工具自动执行。方案见 `plan/PoC_Workflow_Plan.md` 7.3 节。

| 文件 | 作用 |
|---|---|
| `setup-host.sh` | 主机配置，**用 sudo 运行一次**：串口的 udev 规则，USB 网卡的固定 IP，dnsmasq（只在 USB 网卡上提供 DHCP 和 TFTP），`/srv/tftp`。加 `--undo` 可以撤销 |
| `evk.py` | 串口控制台（带日志）和 U-Boot 操作。输出格式按 BSP 所用 rev 的 U-Boot 源码核对过 |
| `evk-write-sd` | 写 microSD：准备分块 → 进入 U-Boot → 逐块 TFTP、CRC32、`mmc write` → 读回校验 → 启动确认 |

## 一次性准备（主机）

```sh
sudo bash workflow/board/setup-host.sh
```

## 每次写板子之前（人）

1. USB 网卡接到 Carrier II 上的任意一个 RJ45 口。工具会先试 eth0，不通再试 eth1。
2. Carrier II 上标着 "UART Debug" 的 micro-USB 口（SER3，FT234XD）接到主机。接上后会出现 `/dev/rzg3l-evk-console`。
3. 模块上的 SYS[1] 打到 ON，选择 microSD0。工具只写 SD 卡，检测到 eMMC 会拒绝写入。
4. SW_MODE 设成板子上**现有**镜像能启动到 U-Boot 的模式。例如 microSD 里已有可启动的镜像时，用 eSD 模式：SW_MODE[1:3] = ON ON OFF。

## 写入

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
