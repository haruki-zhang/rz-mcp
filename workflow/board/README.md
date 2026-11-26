# workflow/board/：写板子（第 6 步，B3）

通过串口和直连网口，把 bootloader 和 rootfs 写进 RZ/G3L SMARC EVK，**不需要拔插卡**。上电、复位和拨码由人完成，其余全部由工具完成。
方案和实机记录见 `plan/PoC_Workflow_Plan.md` 7.3 节。

| 场景 | 命令 |
|---|---|
| 1：在 SCIF 下载模式下写 BL2 和 FIP（以及 CM33 固件） | `evk-flash bootloader emmc` 或 `evk-flash bootloader qspi` |
| 2：在 eMMC 启动模式下，把 rootfs 写进 eMMC | `evk-flash rootfs --image …` |
| 1+2：板子上没有 U-Boot 时，从零做成 eMMC 启动 | `evk-flash emmc --image …` |
| 3：QSPI 启动或 eSD 启动用的 SD 卡 | 在主机上用 bmaptool 写卡，不在本工具范围内 |

| 文件 | 作用 |
|---|---|
| `evk-flash` | 命令入口：`doctor`、`bootloader`、`rootfs`、`emmc`、`clean` |
| `media.py` | eMMC 和 QSPI 的写入地址、EXT_CSD、开关设置，**所有地址只写在这一处**，每个值都注明了出处 |
| `evk.py` | 串口控制台（独占、带日志）、BootROM 和 Flash Writer（SUP、EM_W、XLS2、em_secsd）、U-Boot（MMC、网络、TFTP、CRC32）的操作 |
| `evk-verify <poc>` | 执行 `design/verify.toml`（由 `01-design.md` 第 11 节写成），每个验证项有三种执行位置：`linux`（通过串口以 root 登录后执行命令）、`host`（在主机上执行命令）、`boot`（重启后测到 U-Boot、Starting kernel、login 各自的时间）。只按 `expect`、`expect_rc`、`max_seconds` 判定。从板子输出里提取的值可以用于后面的命令；用在主机命令里时，只接受安全字符，并且会加引号。结果写入 `design/03-verification.md` 和 `design/verify-logs/` |
| `evk-watch` | 跟踪当前占用串口的上板任务（evk-flash、evk-verify，或者 PoC 自己的上板脚本），每个事件输出一行，任务结束时退出。Agent 在后台运行上板命令时，用它（通过 Monitor）把 `>>> 需要你操作` 转告用户。`--quiet` 只输出需要注意的事件，`--status` 显示当前状态 |
| `progress.py` | 进度显示、`status.json`、事件记录 `*.events.jsonl`，以及当前任务的登记（`data/board-runs/active.json`） |
| `setup-host.sh` | 主机配置，**用 sudo 运行一次**：串口 udev 规则、USB 网卡的固定 IP、dnsmasq（只在 USB 网卡上提供 DHCP 和 TFTP）、`/srv/tftp`。加 `--undo` 撤销 |

## 用法

```sh
B=workflow/board
IMG=<deploy>/<image>-smarc-rzg3l.rootfs-<time>.wic.gz       # 同一目录下要有 .wic.bmap，以及 Flash Writer 和启动文件

$B/evk-flash doctor --image $IMG [--target qspi]   # 每次写之前先运行：主机、串口、网口、文件一致性。不写任何东西
$B/evk-flash emmc --image $IMG                     # 板子上没有 U-Boot 也可以：bootloader + rootfs
$B/evk-flash bootloader qspi --image $IMG --boot-check uboot
$B/evk-flash rootfs --image $IMG                   # 板子已经能从 eMMC 启动，只换 rootfs
$B/evk-flash clean                                 # 只保留最近一个镜像的 TFTP 分块（每个约 560 MB）
```

`--image` 对 `bootloader` 是可选的，但建议加上。eSD 的 WIC 在 768s 处内嵌 fip，在 4992s 处内嵌 CM33 固件，工具会把这两段和要写入的文件比对。这样既证明 bootloader 和 rootfs 来自同一次构建，也能确定该用哪个 CM33 固件。比对不一致时，工具在碰板子之前就停下。

每次运行都会生成 `data/board-runs/<时间>-<命令>/` 目录，里面有三个文件：
- `console.log`：完整的串口日志；
- `report.json`：机器可读的运行记录；
- `report.md`：写了什么、写到哪里、sha256、各组件版本（Flash Writer、BL2、BL31、U-Boot）、每一步的耗时和结果。

## 进度显示

- **在终端里直接运行**：进度显示在同一行原地刷新，内容是步骤 `[i/n]`、进度条、已传和总共的字节数、速度、本步剩余和全部约剩的时间（全部约剩不包括等人操作的时间）。
  - 发送 Flash Writer、写 BL2、FIP、CM33：按已经发出的字节算；
  - TFTP：按 U-Boot 打印的 `#` 实时换算成字节（每 10 个块打一个，每传完一块按实际字节数重新校准）；
  - 读回校验：按块算。

  每完成一步打印 ✓ 和耗时。板子启动时的各个阶段（BootROM、Flash Writer、BL2、BL31、U-Boot、Starting kernel、login）一出现就列出来。
- **等人操作时**：用黄色方框醒目提示，并发出提示音，同时显示已经等了多久。板子一有输出，就打印"✓ 检测到板子有输出"，表示你的操作已经被识别。
- **输出不是终端时**（例如 Agent 在后台运行，或者输出重定向到文件）：不在原地刷新，每一步开始时打一行；进度每到 10%，或者每隔 30 秒，打一行。
- **随时查看状态**：每次运行都会实时写 `status.json`。在另一个终端里运行 `evk-flash status`，可以看当前步骤、进度、剩余时间，以及是否在等人操作；加 `--watch` 会持续显示到运行结束。
- 设置 `NO_COLOR=1` 可以关掉颜色。`evk-verify` 用的是同一套进度显示。

## Agent 在后台运行上板命令（evk-watch）

上板命令都由 Agent 运行，人只做物理操作（拨码、上电或断电、RESET、插拔卡和线）。做法是两步：

1. 用后台模式启动上板命令；
2. 紧接着用 Monitor 运行 `/usr/bin/python3 workflow/board/evk-watch`。

`evk-watch` 的输出：

| 输出 | 含义 |
|---|---|
| `>>> 需要你操作：…` | 原样转告用户。工具会从串口判断操作是否完成，用户不需要在对话里回复 |
| `✓ 检测到板子有输出` | 上面的操作已经完成 |
| `[i/n] 标题`，`✓ / ✗ 标题 — 摘要` | 步骤开始，以及它的结果 |
| `• …` | 板子启动的各个阶段 |
| `完成：…` / `失败：…` | 任务结束 |

退出码：0 成功；1 失败；2 表示没有任务，或者进程意外消失（例如被强行结束）。

**它是怎么跟上任务的**：只要通过 `evk.Console` 打开了真正的 EVK 串口，任务就会把自己登记到 `data/board-runs/active.json`，并在 `status.json` 旁边写出事件记录 `*.events.jsonl`。串口是独占的，所以同一时间只会有一个任务。用假板子（pty）测试时不会登记，除非设置了 `RZ_BOARD_ACTIVE`。

**PoC 自己的上板脚本**（例如 Step 0 实验、U-Boot 阶段的检查）不需要另外写什么：

```python
import sys; sys.path.insert(0, "/home/mpu-epmaj/max/workspace/rz-mcp/workflow/board")
import evk
c = evk.Console(evk.PORT, log="design/step0-logs/s0.log")    # 状态和事件写在 s0.status.json 旁边
c.stop_autoboot(600, "拨码设成 eMMC 启动（SW_MODE[1:3] = ON OFF OFF），然后上电。")
print(c.cmd("md.l 0x11861540 4"))
c.ask_human("拔掉 USB-C 电源，等 10 秒再插上。")             # evk-watch 会转告用户
...
c.result(True, "S0-2 通过")                                  # 可选：不调用时，进程退出就算结束
```

脚本自己没有设置进度显示时，`Console` 会在板子第一次有输出时自动建一个。没有调用 `result()` 的，进程退出时按以下规则收尾：有未捕获的异常记为"失败"；否则记为"进程已退出，没有报告结果"，结论看脚本自己的输出和退出码。上板之前，先用 `eval/fake_uboot.py` 把脚本测一遍。

## 需要人做的操作

工具会打印 `>>> 需要你操作：…`，然后通过串口判断操作是否已经完成，不需要在键盘上应答。
它一启动会先判断板子处在什么状态：停在 U-Boot 提示符，还是 Flash Writer 提示符（921600 或 115200）。如果是，就从这一步接着做，不需要人再操作一遍。

| 模式 | SW_MODE[1:3]（Carrier II） | 模块上的开关 | 什么时候用 |
|---|---|---|---|
| SCIF 下载 | OFF ON OFF | BOOT、SYS 全部 OFF（出厂默认） | 写 bootloader |
| eMMC 启动 | ON OFF OFF | SYS[1] = OFF；Carrier 上的 microSD1 槽不插卡 | 写 rootfs，以及之后的正常启动 |
| QSPI 启动 | OFF OFF OFF | SYS[1] 决定 rootfs 从哪里读：OFF 读 eMMC，ON 读 microSD0 | QSPI 写完后的启动确认 |

SW_MODE[4] 和 VBUS_SEL 按电源适配器设置。接线只需要三根：USB-C 电源、"UART Debug" micro-USB、USB 网卡接到任意一个 RJ45 口。

## 出问题时

| 现象 | 原因和处理 |
|---|---|
| 一直等不到 BootROM | 板子在工具启动之前就上电了，BootROM 的提示已经打印过。按一次 RESET。如果确定板子正停在 BootROM 等数据，可以加 `--bootrom-waiting` |
| `No valid MAC address found` | EVK 本身没有 MAC 地址。工具会临时设置一个本地管理型 MAC，只放在内存里，不会保存 |
| ping 失败 | 先看 `doctor` 里的 `ethernet link` 一项。没接网线的口要等 PHY 自协商超时，之后 U-Boot 会自动换到另一个口 |
| `cannot open … 正在使用串口` | 有别的工具或终端程序（minicom、screen 等）占着串口，先把它关掉 |
| 写官方镜像时报 "embeds a CM33 firmware" | 2026-11-26 之前的版本会把官方 WIC 第 4992 扇区的分区数据当成 CM33 固件，必须加 `--cm33 none`。现在会先读分区表：落在分区里的扇区不算嵌入的固件 |
| 中途断了 | 直接重新运行同一条命令：板子停在 Flash Writer 或 U-Boot 提示符时，工具会从那里接着做；TFTP 分块会复用 |

## 测试

```sh
/usr/bin/python3 eval/test_board.py [--real-image $IMG]
/usr/bin/python3 eval/test_watch.py      # 后台运行 + evk-watch：PoC 脚本、evk-flash、evk-verify
```

测试对着 `eval/fake_uboot.py` 运行，它模拟的板子包括 BootROM、Flash Writer、U-Boot、eMMC 和 QSPI，提示文字取自实机日志。测试里有一个"模拟操作员"线程：看到工具打印的请求，才按请求里写的启动模式给假板子上电。不需要真板子，也不需要 sudo。

## 实机验证状况（2026-11-25）

| 流程 | 状况 |
|---|---|
| eMMC 启动（BL2、FIP、CM33 固件、rootfs） | 已在实机上验证：从没有 U-Boot 的状态开始，到出现 `login:` |
| QSPI（`XLS2`） | 只在离线测试中通过。对话流程按 Start-up Guide §4.4 编写，首次实机运行时要确认 Flash Writer 的实际输出 |
