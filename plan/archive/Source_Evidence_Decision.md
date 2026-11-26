# BSP 源码证据：需求、实验与决定（2026-11-26）

问题：agent 要核对"某个驱动 / 内核 / bootloader 在这次 RZ/G3L 构建的板子上实际怎么工作"，MCP 必须提供哪些源码层面的证据？`packages/` 里的授权包要不要纳入、怎么纳入？

## 0. 结论

- **不需要把整个 Yocto 项目放进 MCP。** 按需读 git 足够快（p50 7–13 ms），也不需要建索引。
- **需要改的是"读到的是哪一份代码"。** 原来的工具读的是裸 rev，它既不是官方发布实际编译的代码，也不是这次构建的代码：
  - 可选组件会给内核、TF-A 打官方补丁：graphics 和 multi-os 给内核加 3 个补丁和 1 个配置片段，multi-os 给 TF-A 加 5 个补丁；
  - Mali 驱动在 tarball 之上还有 9 个补丁；
  - 驱动是否被编译、设备树节点是否启用，光看源码答不对。
- **采用的方案：**
  - 源码快照（`workflow/build/src-snapshot`）：每个组件生成一个"release 视图"，即 rev 或 tarball 加上官方配置实际打的补丁，存成 git tree 对象。实测与 bitbake do_patch 的结果逐字节一致，4 个组件全部吻合。
  - 官方构建产物：`.config`、编译对象清单、编译后的设备树。
  - 解析后的 recipe 变量。
  - 新增工具 `build_facts`、`bsp_recipe`；`bsp_source` / `bsp_grep` 增加 `view` 参数；`patch_stack` 改为只列实际打上的补丁。
  - 改动量：新文件 679 行（snapshot.py、src-snapshot），已有文件 +499/−86 行，测试 214 行。
- **packages**：graphics 和 codecs 的源码部分（mali、uvcs）已经通过快照提供。security 包目前一点都没有被提供：它不在任何项目里，PDF 也不在 manifest 里。怎么处理留给你决定（第 7 节）。

## 1. 需求

原始材料：
- `data/calls.jsonl` 里的 321 次 `bsp_*` 调用；
- evk-mac PoC 的三个设计 Agent（两轨方案和融合）、主会话、rzg3l-bsp-v1 和 rz-mcp 的相关会话记录；
- `rzg3l-evk-mac/design/` 里的调研笔记。

完整分类见 `/tmp/demand/questions.json`（124 个问题，每个都附原始记录的文件和行号）。

| 类型 | 用 bsp 工具的问题（调用数） | 只靠 shell 的问题 | 合计 |
|---|---|---|---|
| 1 代码在哪 / 写了什么 | 36 (103) | 1 | 37 |
| 2 是否编译 / 是否启用（Kconfig、.config） | 21 (88) | 21 | 42 |
| 3 DT 节点 / 属性 | 7 (39) | 4 | 11 |
| 4 调用链 | 16 (83) | 2 | 18 |
| 5 为什么改过 / 补丁 | 4 (8) | 4 | 8 |
| 6 其他（WIC 布局、过去的日志……） | 0 | 8 | 8 |

- 73 个问题（59%）只靠官方源码就能回答。设计文档里 83 条引用 kernel/U-Boot/TF-A 的地方，全部能追溯到官方 rev 上的 `bsp_*` 读取。
- 26 个问题需要"这次构建"的事实：`.config`、编译后的 DT、解析后的 recipe。设计阶段还没有构建，只能从 Kconfig 默认值推断；构建之后再从 shell 读 `build/tmp/work*`。
- 只靠 shell 读的内容：layer recipe（16 个问题，meta-renesas 14 次、poky 7 次）、systemd 源码（4）、busybox 配置（5）。

**失败案例**（每条都有原始记录）：

| # | 现象 | 原始记录 | 缺的是什么 |
|---|---|---|---|
| B1 | `bsp_source` 的窗口停在函数第 205 行，下面两行决定了行为；设计里的 S-14 因此写错，到板上 C4 运行时才发现 | M:196（start 190，16 行）→ MAIN:1764 | 读到函数末尾 |
| B2 | `patch_stack` 列出了 9 个 poky CVE 补丁和 hihope 补丁，笔记写成"U-Boot 打了这些补丁"；实际上一个都没打 | A:291 / B:162 / M:526 → MAIN:712 | 解析后的 SRC_URI |
| B3 | path 写错或只写了一部分时，`bsp_grep` 返回"(no hits)"，和真正没有命中分不开 | A:144、B:205、B:221、B:436、B:824 | 检查路径是否存在 |
| B4 | "STRICT_DEVMEM 开了吗"：defconfig 里没有命中，只能按 Kconfig 默认值推断 | MAIN:469、A:577、M:453 | 构建出的 .config |
| B5 | U-Boot 的 EVENT、SHA256 等：设计阶段没有 .config（被 rm_work 删了），构建后才从 shell 读到 | B:314/317 → MAIN:1043 | 构建出的 .config |
| B6 | systemd、layer recipe、multi-os、CM33：工具不支持，只好去 shell 读 | B:467、A:301…、`c153e4fd`:574 | 更多组件和层 |
| B8 | 用了过去 PoC 构建的 DTB（两个以太网节点都是 disabled），标注了"仅用于路径" | B:692、B:705 | 官方构建编译出的 DT |
| B9 | 旧的 v1 笔记引用了 work-shared/kernel-source；`2187a2fb`:866 的 PoC .config 里 STMMAC_ETH 没有开，而官方 defconfig 是开的 | MultiCore-Display 笔记:8 | 区分官方与 PoC |

## 2. 覆盖盘点

先用 `bitbake -e <recipe>` 解析：在 scratch 构建目录里运行，配置是官方模板加 graphics、codecs、multi-os；security 层另外单独做了一次。

| recipe | 源码来自 | 官方补丁 | 以前 MCP | 现在 | 需求 |
|---|---|---|---|---|---|
| linux-renesas | git rz_linux-cip @e6c894b | +3 补丁 +1 cfg（graphics 1，multi-os 2） | 裸 rev | release 视图、.config、编译对象、DT | 66 次调用 |
| u-boot | git renesas-u-boot-cip @16e6324 | 官方 0；装了 multi-os 时 +2 | 裸 rev | release 视图、.config | 218 次 |
| trusted-firmware-a | git rzg_trusted-firmware-a @c06209f | 装了 multi-os 时 +5（AWO 挂起等）；装了 security 时 +1 | 裸 rev | release 视图；make 变量见 `bsp_recipe` | 37 次 |
| kernel-module-mmngr / mmngrbuf / vspm / vspmif | git（renesas-rz） | 0 | 无 | release 视图（要求项目的 DL_DIR 里有镜像：v1 有，rzg3l-bsp 没有取过） | 0 |
| kernel-module-mali | tarball，在 meta-rz-graphics 里 | 9 | 无 | release 视图（tarball + 9 个补丁） | 0；但 DT 实验表明 grep 会指向错误的驱动 |
| kernel-module-uvcs-drv | tarball，在 meta-rz-codecs 里 | 0 | 无 | release 视图 | 0 |
| open-amp / libmetal | git（multi-os） | 各 1 | 无 | release 视图 | 0 |
| flash-writer、cm33-firmware | 层里的预编译 .mot / .elf / .bin | — | 无 | 标明"只有二进制，发布里没有源码" | 1 次 shell（过去 PoC 的副本） |
| mali-library、codec-user-module、libsecureip | 预编译 .so / .a（CLOSED） | — | 无 | 不提供（只有二进制） | 0 |
| optee-os | git rzg_optee-os 4.8.0/rz | — | 无 | 装了 security 后自动提供（目前没有任何项目装） | 0 |
| 层：meta-renesas、poky、meta-arm、meta-oe、meta-rz-* | git checkout 或 tarball | — | 无 | 作为组件提供（git 层按 pin 读取；v1 是 tarball，从磁盘导入，跳过 >1 MB 的文件和二进制文件） | 16 个 shell 问题 |
| busybox、systemd、core-image-minimal | poky | — | 无 | 只提供解析后的变量（`bsp_recipe`），不提供源码 | busybox 5、systemd 4 |

## 3. 实验

### 3a 按需读取还是建索引

原始数据是 `data/calls.jsonl` 里的 `ms` 字段：

| 调用 | 次数 | p50 | p90 | 最大 |
|---|---|---|---|---|
| bsp_grep kernel | 41 | 7 ms | 17 ms | 6439 ms |
| bsp_grep u-boot | 99 | 7 ms | 256 ms | 281 ms |
| bsp_grep tf-a | 21 | 13 ms | 47 ms | 122 ms |
| bsp_source（三个组件） | 162 | 7–8 ms | 9–14 ms | 368 ms |

- 超过 5 s 的只有 3 次，都是不带 path 的全内核 grep。在缓存已热的情况下回放这 3 个查询，`git grep` 各需 1.49 / 1.53 / 1.54 s。
- 如果只在编译过的 .c 文件里 grep，时间降到 0.2 s，但 dts、Kconfig 和头文件里的命中会全部丢失（例如 `otp@` 从 21 个命中变成 0 个），所以不能作为默认行为。
- 主机和容器里都没有 ctags、cscope 或 clangd。18 个调用链问题都靠 grep 答对了，需求分析里没有一例失败可以归因于"缺少索引"。

**推荐：不建索引。**

### 3b tarball 源码

`scratch/srcev/scripts/tarbench.py`：

```
mali_km: tar 1.30 MB -> extracted 6.5 MB | on-demand read 31 ms, on-demand grep 114 ms | extract once 99 ms, cached read 0.03 ms, cached grep 19 ms
uvcs:    tar 0.05 MB -> extracted 0.4 MB | on-demand read 22 ms, on-demand grep 22 ms  | extract once 28 ms, cached grep 3 ms
```

两种方式都够快，起决定作用的另有原因：mali 在 tarball 之上还有 9 个补丁（`striplevel=5`，在 `S=mali_km/drivers/gpu/arm/midgard` 里打）。按需读出的是没打补丁的代码，并不是实际编译的版本。

**推荐：** 拍快照时导入一次，打好补丁，存成 tree。读取速度和 git 组件一样，内容也是打过补丁的。

git 来源的树外模块（mmngr、vspm……）直接沿用 component 机制即可。

### 3c "编译了什么"：用构建产物回答，还是只靠 grep

- **DT 节点 → 驱动 → 是否编译**（`dtdrv.py`）：BR-0 编译出的 DTB 里有 104 个已启用的节点。
  - 71 个由已编译的驱动独占；
  - 21 个是通用 compatible（如 fixed-clock），grep 会同时命中没编译的驱动，属于噪声；
  - 11 个在内核树里找不到驱动：4 个 CPU 节点，其余是 vcp4/isum（codec）和 mmngr；
  - 1 个答错：GPU 节点 `renesas,r9a08g046-mali`。grep 只找到 panfrost，但 panfrost 没有编译；实际的驱动是 tarball 里的 mali kbase。
- **`#ifdef`**（`ifdef.py`）：71 个已编译的 RZ 驱动文件里，被 `.config` 剪掉的代码只有 8 个文件、共 205 行，主要是 SUPERH、RISCV、X86 分支。所以问题在"整个文件是否编译"，不在 `#ifdef`，不需要 `make X.i`。

**grep 给出错误答案的真实案例**（要求至少 5 个，实际找到 7 个）：

| # | 问题 | grep 的结果 | 正确答案和依据 |
|---|---|---|---|
| 1 | GPU 由哪个驱动处理 | panfrost | mali kbase（树外驱动）；panfrost_drv.o 不存在；板上 gpu 节点没有绑定任何驱动 |
| 2 | codec 节点 `renesas,vcp4-vcpl4` 由谁处理 | 内核树里 0 命中；在 uvcs 源码里搜完整字符串也是 0 命中 | uvcs，compatible 由宏 `UVCS_DT_COMPAT_BASE "renesas,vcp4-"` 拼出来 |
| 3 | mmngr 节点 | 内核树里 0 命中 | mmngr_drv.c:2005（树外驱动） |
| 4 | STRICT_DEVMEM 开了吗（需求 B4） | defconfig 里 0 命中，只能推断 | `.config` 里是 `=y`；板上 `/proc/config.gz` 也是 `=y` |
| 5 | U-Boot 的 EVENT、SHA256 等（B5） | 看 defconfig 无法确定 | 构建出的 .config 里是 `=y` |
| 6 | 这块板支持以太网启动吗（B9） | 官方 defconfig 开了 STMMAC_ETH | 当时项目里 PoC 构建的 .config 是 `# CONFIG_STMMAC_ETH is not set`（`2187a2fb`:866 / 871） |
| 7 | 这块板上编译了 CAN-FD 吗 | 源码和官方配置都说是 | 官方是；v1 的 PoC 构建里没有（见 3e） |

### 3d DT：编译出的 DTB 还是源码

`dtstat.py`：在官方 rev 的 5 个 dts/dtsi 里找出每个 label 的定义和所有 `&label` 覆盖，再和编译结果对比。编译结果用 `dtc -I dts -O dts` 处理预处理后的 `.r9a08g046l48-smarc.dtb.dts.tmp` 得到，这样 label 能保留下来。

```
113 device labels
只看定义节点的那个 dtsi：  42 个答错（def disabled -> final okay，例如 i2c0/1/2/3、scif0、adc0）
跟踪所有 &label 覆盖但忽略 #if：  5 个答错：i3c、sdhi2、mtu3、poe3、spdif
（这几个的覆盖写在 #if SW_I3C_EN / SW_SD2_DEV_SEL 等分支里；板级 dts 有 44 处条件编译）
编译出的 DTB：0 个答错（按定义如此），见第 6 节的板上核对
```

只读源码时，要看对 status，必须读 2–3 个文件，还要自己算 cpp 开关；56 个节点需要 2 个以上的文件。

**推荐：** 用编译后的设备树回答"启用了吗"，也就是 `build_facts kind=dt`。

### 3e 拿到官方基线的 .config / .o / DTB

用 `.config` 的符号差异（`cfgdiff.py`）和对象清单的差异来比较：

| 比较 | .config 符号 | 编译对象 |
|---|---|---|
| BR-0（官方，rzg3l-bsp）vs 官方 + graphics/codecs/multi-os | 3 个（CONFIG_UIO、UIO_DMEM_GENIRQ、UIO_PDRV_GENIRQ） | — |
| 官方 + 可选组件 vs v1 的 PoC 构建 | **364 个**（CAN、BTRFS、crypto……） | v1 少 **906** 个、多 5 个（其中有 `rzg2l_du_lpdemo.o`） |

几种拿到基线的办法：
- 直接用一个干净项目的构建产物：只有项目里没有 PoC 改动内核时才成立（rzg3l-bsp 可以，v1 不行），而且得先完整构建一次（48 分钟）。
- **代价最小的办法**：建一个 scratch 构建目录，`SSTATE_MIRRORS` 指向项目的 sstate-cache。
  - 干跑：`Sstate summary: Wanted 228 ... Mirrors 221 Missed 7`；
  - 实际：`do_configure` 122 s，`do_compile` 187 s，v1 上 `--artifacts` 整个过程 272 s；
  - 临时占用约 6 GB，最后只保留 `.config`、对象清单和 DTB，不到 1 MB。

### 原型：release 视图（不在原题里，但起了决定作用）

`view.py`：rev（或 tarball）加上 `bitbake -e` 解析出的 SRC_URI 里的补丁，在旁路仓库里生成 tree，然后和 scratch 构建的 do_patch 结果比较：

```
kernel  base tree aac4e28f  patches 3  view tree 3251351f…  0.27s   reference (work-shared kernel-source HEAD^{tree}) IDENTICAL
tf-a    base tree c20ebf58  patches 5  view tree 1f85f152…  0.07s   reference (WORKDIR/git) IDENTICAL
u-boot  base tree 2ebe0450  patches 2  view tree dd16b4d7…  0.13s   reference IDENTICAL
mali    base tree d472772f  patches 9  view tree 3e1535d8…  2.05s   reference (S=mali_km/drivers/gpu/arm/midgard) IDENTICAL
4 个视图共 5.1 MB；读取延迟与原来相同（git grep 5–13 ms）
```

实验中还发现：在带 alternates 的旁路仓库里运行 `git write-tree` / `apply --cached`，会"刷新"（touch）项目 git 镜像里 pack 文件的 mtime；内容没有变化，pack 的名字就是内容的哈希。bitbake 的 do_fetch 也会 touch DL_DIR 里的 `.done`。这次是在 rzg3l-bsp 上发生的，没有涉及 v1。正式实现因此做了三件事：
- 只写入镜像里没有的对象；
- 用 scratch DL_DIR 加 PREMIRRORS（`file://` 方式克隆，git 会复制，不用硬链接）；
- bitbake 从 `poky/bitbake` 的副本运行，因为它每次都会在自己的代码旁边重新生成被 git 忽略的 `pyshtables.py`。

## 4. 方案与实现

- **`workflow/build/src-snapshot [--project DIR] [--artifacts]`**：
  - 在 `data/snapshots/<项目>/` 里写入 `snapshot.json`、`views.git`（alternates 指向项目的镜像，只存补丁产生的新对象）、`local.git`（tarball 和非 git 层）、`env/*.env.gz`、`kernel/`、`u-boot/`；
  - 实测：v1 用时 120 s，`--artifacts` 再加 272 s，占用 25 MB；rzg3l-bsp 用时 105 s，`--artifacts` 再加 396 s（它的 sstate 里没有 glibc，要从 DL_DIR 的 tarball 现编），占用 4.7 MB；
  - 每个视图都有 ref 指着（`refs/rz/<组件>/<视图>`），误运行 `git gc` 也不会丢；最后做一次 repack（views.git 用 `-l`，不复制镜像里的对象）；
  - 运行前后用 `find -newer` 检查项目：**0 个文件被写入**。
- **`bsp_source` / `bsp_grep`**：
  - `view="release"`（默认）或 `"rev"`；组件来自快照；会说明是哪个官方补丁改了这个文件；
  - 路径不存在时报错，只写了一部分的路径当作前缀，可以一次给多个路径（B3）；
  - 读取窗口停在块中间时给出提醒（B1）；
  - 修了一个以前就有的 bug：输出超过 30000 字符时会被静默截断，而标题仍写"lines 1-854 of 854"。这是新测试发现的。
- **`patch_stack`**：按顺序列出解析后的 SRC_URI，带层和改动的文件；PoC 层的补丁另列（B2）。
- **`build_facts`**（新）：`config`（内核或 U-Boot）、`compiled`、`dt`（B4、B5、B8、3c、3d）。
- **`bsp_recipe`**（新）：解析后的变量，以及每次赋值的文件和行号（B6 里的 layer recipe 问题、SH-X1、V4）。
- **`check_build_env`**：报告快照是否存在、是否过期。快照过期由层的指纹判断：git 层看 HEAD，非 git 层看文件大小和 mtime。
- **catalog**：`[releases.*.sources]` 列出提供哪些组件；以后要加 systemd 之类，只需加一行。

## 5. packages 的内容和 MCP 的覆盖

| 包 | 内容 | MCP 现在 |
|---|---|---|
| RTK0EF0045Z14001ZJ graphics 5.1.3.2 | 授权协议 PDF（日文）、Readme；`meta-rz-features-5.1.3.2.tar.gz` 里有层（44 个文件）：mali km 源码 tarball（514 个文件：184 个 .c、231 个 .h）+ 9 个补丁；mali um tarball 98 MB（11 个 .so 和头文件，预编译）；内核补丁 0004（GPU power-domains） | 层和 mali km 的 release 视图（装了 graphics 的项目才有）；um 只有二进制，不提供 |
| RTK0EF0045Z16002ZJ codecs 5.1.5.0 | 授权协议 PDF、Readme；`meta-rz-features_codec` tar（27 个文件）：uvcs 内核源码 tarball（31 个文件：8 个 .c、14 个 .h）、codec_pkg_product（预编译 .so 和头文件）、codecinfo；内层 zip 里是 prebuilt tarball | 层和 uvcs 的 release 视图；预编译部分不提供 |
| RTK0EF0121Z00000ZJ security 6.12-v1.0 | 3 份 PDF：R11AN1073EJ0110 Security Startup Guide（40 页）、R11AN1074EJ0100 Security Manual（80 页）、R11UT0130EJ0100 Release Note（9 页）。`meta-rz-features_security.tar.gz`（120 个文件）：optee 的 recipe（optee-os 来自 GitHub rzg_optee-os 4.8.0/rz）；TF-A 补丁（secure boot api v2）；flash-writer 补丁（TSIP key injection）；内核 dts 补丁（optee 节点）和 optee.cfg；libsecureip（预编译）；TA 示例；签名用的 class。`security_configurator.tar.gz`：140 个文件，写 OTP 的裸机程序的 C 源码。`manifest_generation_tool.tar.gz`：Python 工具，外加第 4 份 PDF R11AN1043EJ0110 Manifest Generation Tool（27 页）。`sample_key/`（ufpk.key、wrapped-ufpk.key） | **全部没有提供**：没有任何项目装了这个层；PDF 不在 manifest 里 |

4 份 PDF 都没有标 CONFIDENTIAL（文中的 "confidentiality" 是安全术语）。但这个包需要 Renesas 批准才能获取。sample_key 无论如何都不应该提供。

**manifest 草稿**（没有写进 manifest，也没有建索引）。两种写法都要先把 PDF 从 zip 解压到 `docs/devices/rzg3l/security-package/`：

```toml
# 方案 A：只登记，不建索引（与机密版 HW UM R01UH1152EJ0110 的处理相同）
[[documents]]
id = "R11AN1073EJ0110"
title = "RZ/G3L SMARC EVK Security Startup Guide"
doc_type = "app_note"
rev = "1.10"
path = "docs/devices/rzg3l/security-package/r11an1073ej0110-rzg3l-security-startup-guide.pdf"
pages = 40
devices = ["rzg3l"]
boards = ["rzg3l-evk"]
confidentiality = "restricted"        # 不标 CONFIDENTIAL，但需要 Renesas 批准才能获取（RTK0EF0121Z00000ZJ）
index = []
outline = "bookmarks"
note = "From the Security Package RTK0EF0121Z00000ZJ-11-6.12-v1.0. Registered only."
# R11AN1074EJ0100（Security Manual，80 页，app_note，rev 1.00）、R11UT0130EJ0100（Release Note，9 页，sw_manual，rev 1.00）、
# R11AN1043EJ0110（Manifest Generation Tool，27 页，app_note，rev 1.10，在 manifest_generation_tool.tar.gz 里）写法相同

# 方案 B：建索引（search_docs / get_section 可以查到）
#   与 A 只有两处不同：index = ["fulltext"]；note 写明 "Indexed; do not quote in customer deliverables
#   without checking the customer has the Security Package"
```

## 6. 验证

不靠我自己判断，参照物都来自 rz-mcp 之外。测试文件是 `eval/test_src_snapshot.py`，45 项全部通过；原有的 `eval/test_b1c_b2.py` 38 项也全部通过。

- **逐字节一致**：
  - rev 视图 = `git show <rev>:<path>`；
  - 打过补丁的文件 = `git show` 加 GNU `patch`（不是 git apply）；
  - mali = `tar -xzO` 加 GNU `patch` 打 5 个补丁；
  - 4 个组件的 release tree = bitbake do_patch 的 tree 哈希；
  - rzg3l-bsp 的 release kernel = 它自己 BR-0 构建的 kernel-source。
- **构建产物与项目自己的官方构建一致**：在 scratch 里构建出的 rzg3l-bsp 内核 `.config`、DTB 与 BR-0 的逐字节相同，编译对象清单 3445/3445 完全一致。
- **project 只读**：views.git 里的 57 个松散对象，没有一个与内核镜像里已有的对象重复；运行前后用 `find -newer` 检查，项目没有任何文件被写入。
- **15 道 known-answer 题**，答案来自构建产物、板子和 PoC 记录：
  - STRICT_DEVMEM；U-Boot 的 EVENT、SHA256、OF_SYSTEM_SETUP、DM_SEQ_ALIAS；
  - i3c、sdhi2、i2c0、gpu 的 status；
  - panfrost 是否编译；CAN-FD 是否编译；UIO；
  - mali、uvcs、mmngr 的驱动位置；
  - UBOOT_CONFIG；KBUILD_DEFCONFIG 和 KERNEL_DEVICETREE。
- **EVK 只读核对**（`scratch/srcev/scripts/evk_ro.py`，通过串口执行 shell，不写板子）。当时板上运行的是官方内核 `6.12.46-cip8-...-ge6c894b7436f`：

```
i3c@100e0000 status=disabled     mmc@11c20000 (sdhi2) status=disabled   timer@10001200 (mtu3) status=disabled
i2c@100ae000 status=okay         gpu@108b0000 status=okay               can@100c0000 status=okay
/sys/bus/platform/drivers: rcar_can rcar_canfd（100c0000.can 已绑定）；没有 panfrost；108b0000.gpu 没有绑定驱动
/proc/config.gz: CONFIG_CAN_RCAR_CANFD=y  CONFIG_STRICT_DEVMEM=y  （没有 CONFIG_UIO：这个镜像没有装 multi-os）
```

  以上三类结论（DT 的 status、驱动绑定和是否编译、.config）都与 `build_facts` 的预测一致。其中 i3c、sdhi2、mtu3 恰好是"只读源码会答错"的那几个节点。

## 7. 留给你决定（实验解决不了）

1. **security 的 4 份 PDF**：按什么机密等级处理（没标 CONFIDENTIAL，但需要批准才能获取）；用方案 A 只登记，还是用方案 B 建索引；能不能出现在客户交付物里。
2. **security 包里的其他内容**：装了 security 层后，optee-os（GitHub 上公开的 git）会自动提供。security_configurator 的 C 源码和 manifest 工具要不要提供？sample_key 我建议永远不提供。
3. **RZ 专有代码之外的通用包**：现在提供了 poky、meta-oe、meta-arm 的 recipe（元数据），以及 busybox、systemd 解析后的变量。要不要连 systemd、busybox 等的**源码**也提供（需求里 systemd 4 次、busybox 5 次）？加的话只需在 catalog 里加一行。
4. **要不要给 agent 开放带 PoC 补丁的视图**：也就是本项目上次构建的 work-shared 源码、`.config` 和 DTB。需求里 26 个"这次构建"的问题，有一部分是构建之后才问的（SH-X5/V6/V7/D1/D2），当时是从 shell 读的，并且都做了标注。可以加一个 `view="build"`，所有输出都标明"含 PoC 改动"。
5. **磁盘预算**：每个快照 4–26 MB；`--artifacts` 运行时临时占用约 6–10 GB，结束后删除。要给几个项目拍快照、要不要保留 artifacts？
6. **自动刷新**：要不要在 `yocto-project setup`、`yocto-project add` 和 `poc-use` 装了新组件之后，自动运行 `src-snapshot`（每次多 2 分钟，带 artifacts 多 6 分钟）？现在是靠"过期"提示让人手动运行。

## 附：原始数据和脚本

- 需求分析：`/tmp/demand/questions.json`、`claims.json`。
- 实验脚本：`/home/mpu-epmaj/max/scratch/srcev/scripts/`（inv.py、cfgdiff.py、dtdrv.py、dtstat.py、ifdef.py、tarbench.py、view.py、evk_ro.py）。
- `bitbake -e` 的输出：`/home/mpu-epmaj/max/scratch/srcev/{base,sec}/env/`；覆盖盘点：`scratch/srcev/inventory.json`。
- 板上的日志：`scratch/srcev/evk_ro.log`。
- 第 3 节的 scratch 构建目录 `scratch/srcev/base` 有 6 GB，可以删除（rm -rf /home/mpu-epmaj/max/scratch/srcev/base /home/mpu-epmaj/max/scratch/srcev/sstate）。
