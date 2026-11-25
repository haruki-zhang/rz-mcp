# RZ/G3L PoC 工作流与 MCP 定位（v0.2）

> 状态：草案。依据与用户对齐的结论（2026-11-25）。
> 适用范围：仅 RZ/G3L。Tesera（RZ/G2L）回放作为后续单独的范围例外处理。

---

## 1. 方向

MCP 服务器的重心从"扩展寄存器覆盖"转向"服务 PoC 开发和 ticket support"。

- **Claude Code** 是执行者：读写源码、运行脚本、撰写文档。
- **MCP** 提供知识和流程定义：文档和寄存器（已有）、版本与源码地图、文档与源码之间的链接、过去 PoC 的知识库，以及工作流 prompts 和模板。
- **脚本** 负责所有会改变状态、耗时长或接触硬件的工作：基线构建、PoC 构建、打包、上板 harness。
- **人** 负责三件事：提出设想（第 1 步）、对齐构想（第 4 步）、接好 EVK 并上电或重启（第 6 步）。

构建本身（包括通过 git 获取各层，以及 bitbake 下载源码）完全由脚本完成，MCP 不参与。
唯一例外是图形和 codec 包：它们需要登录 renesas.com 手动下载，脚本只负责接收文件并校验 sha256。

## 2. 已确定的事项

| # | 决定 | 对设计的影响 |
|---|---|---|
| 1 | 不使用继电器，上电和重启由人操作；可以安装 TFTP 和 NFS 用于文件传输和烧写 | 第 6 步的自动化从"人上电之后"开始：通过串口操作 U-Boot、网络启动（TFTP 加载 kernel 和 DTB，NFS 挂载 rootfs）、执行测试、抓取日志。冷启动、拨码切换启动模式、换 SD 卡都由人完成 |
| 2 | 交付物沿用 rzg3l-fast-boot-seamless-logo 或 rzg3l-lowpower-display 的样式 | `layers/`；`docs/<Guide>.md`，用 `tools/md2pdf.py` 生成 `.pdf`；`image/<variant>/*.wic.gz` 和 `.wic.bmap`；README |
| 3 | 过去的 PoC 可以入库 | 实验期间要按第 5.3 节"留出"相应内容，避免答案泄漏 |
| 4 | PoC 仓库放在同一个 Yocto 项目的主目录下，复用同一个 Yocto 项目和构建目录 | 见第 4 节 |
| 5 | 实验 1 回放 rzg3l-lowpower-display（原定 MultiCore-Display），Tesera 之后再做 | 见第 5、6 节 |
| 6 | 第 1 步先用 in-your-own-words 复述并对齐，再用 sharpen 深化，得到两版设想；第 3 步为两版设想分别补全方案，交给单独的 Agent 比较、取长补短，融合版交给用户对齐 | 见第 3.1～3.3 节 |
| 7 | 两个 skill 保持由用户手动调用 | 3.1.1 只作为备查，暂不实施 |
| 8 | 跳过实验 1，直接搭建流程 | 第 5 节暂缓，保留下来，以后可以作为回归测试。不再需要留出，过去的 G3L PoC 全部入库。改用第 7 节的组件级已知答案测试，再把第一个真实 PoC 当作验收 |
| 9 | TFTP 和 NFS 走 USB 网卡，主机与 EVK 直连（方案 2）。网卡已经备好 | 见 7.3 |

## 3. 六步工作流的分工

| 步骤 | 谁做 | MCP | 脚本 | 产物和关口 |
|---|---|---|---|---|
| 1 提出设想，形成两版 | 人提出设想并输入命令；Claude Code 复述和深化 | `/rz-docs:poc <设想>`：prompt 里写明流程、模板和关口，并提示用户在什么时候输入哪个命令 | `poc-new`：在 Yocto 项目主目录下创建 PoC 仓库骨架 | `00-request.md`（需求原文）；`00a-restated.md`（**v1**，已对齐）；`00b-sharpened.md`（**v2**）。见 3.1 |
| 2 调研（两轨并行） | 两个相互独立的设计 Agent，分别负责 v1 和 v2 | 文档和寄存器查询（已有）；版本与源码地图；`driver_profile`；`boot_flow`；过去 PoC 的检索 | 官方源码从 DL_DIR 的 git 镜像读取（见第 4 节）；需要构建时由主会话串行执行 | 两份调研笔记，每条事实都带出处。见 3.2 |
| 3 补全方案（两轨并行），再比较融合 | 两个设计 Agent 各写一版方案；然后由**单独的比较 Agent** 融合 | 两轨共用同一个设计模板，结构沿用过去 PoC 的可行性分析：结论、HW 事实、方案、改动清单、风险、PoC 步骤、资料缺口、待决定事项、上板验证计划。比较 Agent 用 MCP 核实事实 | 主机测试框架；能用实验回答的问题先做原型 | `01a-design-v1.md`、`01b-design-v2.md`，以及融合后的 `01-design.md` 和 `01-merge-notes.md`。见 3.2、3.3 |
| 4 对齐 | **人** | — | — | 对照融合说明，对融合版做对齐；决策记录打 tag `design-v1`，没有这个 tag 不进入第 5 步 |
| 5 实现与交付 | Claude Code | `get_bsp_release`；交付清单；`patch_stack` | PoC 构建；用 devtool 把改动导回 PoC 层；`package`；洁净室复现 | 交付包和 release tag |
| 6 上板验证 | 人接线、上电，之后由 Claude Code 接手 | 板子准备清单；烧录地址；`locate_log` | 串口 harness；TFTP 和 NFS；Flash Writer 烧录；SD 写卡 | 验证报告，逐项对照设计中的验证计划，附原始日志 |

### 3.1 第 1 步：两版设想

1. 用户提出设想，原样保存为 `00-request.md`。
2. 用户输入 `/in-your-own-words`，Claude Code 用自己的话复述用户的目标和要解决的问题。用户纠正，直到确认为止。确认后的版本就是 **v1**，写入 `00a-restated.md`，用户的纠正也原样记录。
3. 用户输入 `/sharpen`，Claude Code 针对 v1 完成四项分析：
   - 顶尖从业者会做什么；
   - 最原始的材料是什么；
   - 用哪些具体方法和工具；
   - 除你我之外，什么能证明结果是对的。

   然后改写需求，得到 **v2**，写入 `00b-sharpened.md`。v2 不单独对齐，它引入的假设在第 4 步通过融合说明一并确认。

两个约束：

- 两个 skill 都设置了 `disable-model-invocation: true`。这样设置后，只能由用户输入命令触发：Claude 不会自己调用，也不会把它们预加载到子 Agent。所以目前 `/rz-docs:poc` 只负责提示用户在哪一步输入哪个命令。要改成自动调用，前提见 3.1.1。
- 顺序不能颠倒：输入 `/sharpen` 时，对话里必须已经有对齐好的 v1。本会话开头那次没有附带任何内容的 `/sharpen`，就是因为没有可深化的对象而失败的。

#### 3.1.1 让两个 skill 自动调用的前提

1. **取消手动限制，并控制作用范围。** 删掉 `disable-model-invocation: true` 之后，skill 的描述会进入每个会话的上下文。personal skill（`~/.claude/skills/`）对所有项目生效，Claude 在别的项目里也可能自行调用它们。建议的做法是：
   - personal 版保持手动不变；
   - 在 Yocto 项目里另建 project skill（`rzg3l-bsp-v1/.claude/skills/`），只在该项目的会话里生效，也可以提交到仓库给团队共用；
   - **名字必须和 personal 版不同**，因为同名时 personal 版优先。
2. **输入要显式传入。** 两个 skill 的正文都针对"当前这个任务"。自动调用时，要把需求原文或 v1 的文件路径作为参数传进去（`$ARGUMENTS`），否则又会出现空 `/sharpen` 那样的失败。
3. **人工确认不能省。**
   - in-your-own-words 的价值就在于用户的确认，所以自动调用之后流程必须停下来提问，并记录用户的纠正；
   - sharpen 把偏好类的选择留给用户，自动流程必须把这些选择转交给用户，例如并入第 4 步的待决定事项。

   也就是说，自动化只是"自动触发，并强制停下来等人"，而不是跳过人。
4. **用 MCP 不能实现自动调用。**
   - MCP prompts 和 skill 一样，只能由用户以斜杠命令触发，所以它只解决分发问题，解决不了自动调用；
   - 通过 MCP 提供的 skill 倒是可以由模型调用，但需要 Claude Code v2.1.331 或更高版本（当前是 2.1.322），企业账号还要管理员开启。
5. **`context: fork` 只适合 sharpen。** fork 出来的子 Agent 看不到对话历史。sharpen 的输入可以全部通过参数传入，所以可以 fork；in-your-own-words 必须看到用户的原话，所以不能 fork。
6. **改完要验证三件事**：
   - 流程能带着参数调用两个 skill，并写出 `00a`、`00b`；
   - 调用后确实停下来等用户确认；
   - 在无关的会话里不会被误调用。

### 3.2 第 2、3 步：两轨并行

- 两个设计 Agent 并行工作，互相独立：一个拿 v1，一个拿 v2，各自调研、各自写方案。**不共用调研结果**，否则两版方案会趋同，比较就失去了意义。v2 本身就要求从更原始的材料入手，两版需要的调研也不一样。
- 并行受第 4 节约束：大家共用一个构建目录，同一时间只能跑一个 bitbake。所以：
  - 设计 Agent 对构建树和 git 镜像只有读权限；
  - 需要构建时提出请求，由主会话串行执行；
  - 不依赖 bitbake 的主机原型，各自在独立的临时目录里运行。
- 两版方案使用同一个设计模板，方便逐节比较。

### 3.3 第 3 步：比较与融合

- **输入**：比较 Agent 是单独启动的，只拿到 v1、v2 和两版方案的文件，**拿不到**两个设计 Agent 的过程记录，以免被它们的推理带偏。它可以用 MCP 核实内容。
- **判断标准**，按优先级排列：
  1. **事实**：逐条核实两版方案里的 HW 事实和引用，工具用 `read_pages` 和 `lookup_register`。核实不了的标记出来，不进入融合版。
  2. **v1 的覆盖**：v1 里的每一项需求都不能在融合版中丢失。
  3. **v2 带来的深度**：只保留"更深而不是更大"的部分。扩大范围的内容单独列为可选项，交给用户决定。
  4. **风险、资料缺口、待决定事项**：两版取并集，再去重。
  5. **冲突**：能用实验回答的，写成主机原型或 Step 0 实验并给出推荐，简单的可以直接跑；属于偏好或产品层面的，放进"需要用户决定的事项"。
- **产物**：
  - `01-design.md`：融合后的方案；
  - `01-merge-notes.md`：融合说明，包括每一项来自哪一版（A、B、两者都有或新增）、舍弃了什么及理由、冲突如何处理。

  第 4 步用户对齐时，同时查看这两份文件。

## 4. 共用一个 Yocto 项目时的约定

本节以 rzg3l-bsp-v1 为对象。它的层来自 tarball，经核实与 `RZG3L-BSP-1.0.0` tag 基本一致：只少了 tag 上最后一个 audio-init 修复，另有一行本地加的 TF-A 注释。

- **官方源码从 DL_DIR 的 git 镜像读取，不读 work-shared。** work-shared 里的 kernel-source 是最近一次构建的结果，比官方 `KERNEL_REV` 多了 20 个提交，其中包括 PoC 补丁。官方 rev 已在镜像中核实存在，用 `git show <rev>:<path>` 读取，不占额外磁盘空间：

  | 仓库 | 镜像大小 | 官方 rev |
  |---|---|---|
  | rz_linux-cip | 3.2 GB | `e6c894b7`（rz-6.12-cip8） |
  | renesas-u-boot-cip | 261 MB | `16e6324c` |
  | rzg_trusted-firmware-a | 47 MB | `c06209f1` |

  Renesas 的补丁来自 meta-renesas，PoC 的补丁来自各 PoC 层，两者都按来源分组显示。
- **切换 PoC 就是切换配置。** 每个 PoC 仓库都带一个 `build-conf/`（MultiCore-Display 已经有），用脚本 `poc-use <name>` 生成 bblayers.conf 和 local.conf，替代现在手工维护的 `*.conf.<variant>` 文件。
- **deploy 目录会被覆盖。** 所有 PoC 共用一个 TMPDIR，所以构建完成后必须立刻由 `package` 把产物复制到 PoC 仓库的 `image/` 下。
- **要保留的构建树。** `RM_WORK_EXCLUDE` 加上 linux-renesas、u-boot 和 trusted-firmware-a。目前 U-Boot 的 work 目录已经被 rm_work 删掉了。
- 是否把 rzg3l-bsp-v1 的层改成用 git 管理，留待以后决定。

## 5. 实验 1：回放 rzg3l-lowpower-display（暂缓）

> 按决定 8 暂缓。本节保留下来，供以后需要证明 MCP 的价值，或者做回归测试时使用。到那时流程已经搭好，回放只需补上沙箱、按截止日期建的库和核对清单。

### 5.1 目的

衡量这套工作流从当初的设想出发，能在多大程度上得出最终实际交付的方案，并分清每个 MCP 组件以及两轨流程各自的贡献。

实验分两期：

- **第一期**：第 1～4 步，从设想到对齐后的方案。不需要构建，也不需要板子。
- **第二期**：第 5 步，按对齐后的方案实际实现并构建，再和交付物逐个模块对照；第 6 步上板由用户配合。第二期做不做，看第一期的结果再定。

### 5.2 输入和答案

- **输入**（当初的设想）：`RZG3L-LowPower-DisplayPoC.pptx`，6 页，2026-10-09。内容是背景、仿真环境（当时标注为 "PoC not implemented"），以及 RZ/G3L 和 RZ/G3E 的功耗测量结果。
- **最终答案**：rzg3l-lowpower-display，2026-11-19 交付。
  - 指南的三部分：
    - §1 Objective，共 5 个目标；
    - §3 How It Works：关键组件、显示交接、低功耗的进入与唤醒、功耗测量、SRAM 布局；
    - §6 Known Limitations，共 8 条。
  - 实际改动清单：两个层共 24 个补丁，加上 CM33 固件和用户态 demo。

    | 模块 | 内容 |
    |---|---|
    | TF-A | 新增 DISP_RET 挂起模式（1 个补丁） |
    | U-Boot | 以 1080p 启动 logo（lp-display，1 个补丁）；显示驱动和 boot logo（seamless-logo-still，5 个补丁） |
    | kernel | lp-display 的 9 个补丁：保持 RIIC0 时钟，在 DT 中把 RIIC0 和显示交给 CM33，rz-du、vsp1、mipi_dsi 在挂起期间保持运行，CPG 只恢复时钟设置，1080p 输出。seamless-logo-still 的 8 个补丁：接管 bootloader 留下的显示管线，以及 adv7511 相关修改 |
    | CM33 固件 | VSPD、readout、sync、wake、RIIC、ISL28022 功耗测量 |
    | 用户态 | lpdemo-gui、lpdemo-ctl、镜像 lpdemo-image、wks |
- **中间答案**：MultiCore-Display 中关于方案 ② 的分析，用来评估设计阶段的推理，例如资料缺口和待决定事项。
  - 10-13 的可行性分析：§1 共用的 HW 事实、§4，以及 §5 中和 ② 相关的 D 项和 R 项；
  - 11-18 的交互 Demo 可行性与设计。
- **评分时把条目分成两类**：
  - **由事实决定的**：回放应当自己推出来。例如 SRAM 容量限制了低功耗画面只能是两个 720x88 的图层；RIIC0 交给 CM33 以后，Linux 里的音频就不能用了。
  - **由需求方当年的决定得出的**：例如低功耗期间显示什么（对应 R-1）。回放只要**提出了这个待决定事项**就算命中，不要求选得和当年一样。
- **第 1 步只做一次**：在沙箱里由用户走一遍 3.1 的流程，得到 v1 和 v2 后冻结，所有实验臂都用这同一份。这样各实验臂之间只有 MCP 条件不同。
- **事后偏差**：回放时用户扮演需求方，但已经知道当年的结果。所以对齐时只确认"复述是否符合 PPT 当时写的内容"，不补充之后才知道的信息；用户的纠正原样记入 `00a-restated.md`，供事后检查。

### 5.3 留出和隔离

截止时间定为 **2026-10-09**，也就是 PPT 的日期。回放所用的 Agent 和 MCP 实例都不能看到以下内容：

| 泄漏源 | 处理方式 |
|---|---|
| rzg3l-lowpower-display；MultiCore-Display 整个目录（包括 lp-display 层、Step 0、devtools、三份分析文档和两份 PPT） | 不入库，并在沙箱中隐藏 |
| rzg3l-fast-boot-seamless-logo 中截止日期之后的内容：10-27 开始的改名和导入、cm33-anim、11-18 的 lp-display 重命名 | 只入库截止日期之前的状态，即最后一个提交 `2d01408`（2026-09-07）。seamless-logo-still 是 10-27 由已有的层改名而来，其中的内核接管补丁在截止日期时是否已经存在，建库时要逐个核对 |
| rzg3l-bsp-v1/build：kernel-source 里有显示交接的补丁；TF-A 的 work 目录里有 DISP_RET 和 AWO 挂起补丁；conf/ 里有 lpdemo 等配置变体 | 在沙箱中隐藏；官方源码只从 DL_DIR 的 git 镜像读取 |
| meta-renesas 中本地加的 `PLAT_SYSTEM_SUSPEND=awo` 注释。这个 PoC 的核心就是挂起模式，所以这一行等于直接给了提示 | 改用 `RZG3L-BSP-1.0.0` tag 的干净副本 |
| `~/.claude/projects/` 里的历史会话记录 | 在沙箱中隐藏 |
| `~/.claude/history.jsonl`：343 条输入历史，其中 17 条提到 lowpower、lp-display、DISP_RET 或"低功耗"。`~/.claude/` 下还有 file-history、paste-cache、sessions 等目录 | 整个 `~/.claude` 改为白名单，见下文 |
| rz-mcp 仓库本身：本计划书和之后的核对清单都含有答案 | 沙箱里不挂载 rz-mcp，A0 只挂载 `docs/`；MCP 实例在沙箱外运行 |

- **隔离方法：只放行白名单，不靠黑名单排除。** 用 bubblewrap 0.11.1 构造文件视图：
  - 系统目录以只读方式挂载；
  - `$HOME` 换成空的 tmpfs，再只挂载需要的东西：Claude Code 的程序文件、`.credentials.json`、`settings.json`、`skills/`、一份精简过的 `~/.claude.json`（只保留登录信息和实验用的 MCP 配置）、实验工作目录（可写）、DL_DIR 的 git 镜像（只读），以及 meta-renesas tag 的干净副本（只读）。

  另起一个 rz-mcp 实例，换一个端口：A1 直接用现有的库，A2 用按截止日期新建的库。
- **已核实**：
  - 本机虽然开启了 `apparmor_restrict_unprivileged_userns=1`，bubblewrap 仍能以普通用户身份隐藏目录，被隐藏的目录在沙箱里显示为空；
  - 现有库收录的 55 份 PDF 中，没有一份是 2026-10-09 之后生成的，所以 A1 可以直接用现有的库。
- **隔离检查**，开跑前做两项，有一项不通过就不开跑：
  1. 上表中的每个路径在沙箱里都无法访问；
  2. **金丝雀检索**：取"答案里有、但输入 PPT 里没有"的特征词（如 DISP_RET、lpdemo、RIIC0 交接、ISL28022 等），在沙箱可见的整个文件系统中检索，命中必须为零。设计模板和各 Agent 的说明也要用同一组词检查，防止由我写入的内容泄漏答案。

### 5.4 实验臂

| 实验臂 | 条件 | 运行哪些 | 衡量什么 |
|---|---|---|---|
| A0 | 不用 MCP，只把 PDF 和官方源码放在磁盘上 | 只跑 v1 那一轨（方案 A） | 不用 MCP 的基线 |
| A1 | 现有 MCP（文档和寄存器） | 只跑 v1 那一轨（方案 A） | 现有能力的贡献 |
| A2 | A1 加上截止日期之前的过去 PoC 库 | 完整流程：方案 A、方案 B，再融合成 M | PoC 知识库的贡献；两轨加融合的贡献 |
| A3 | A2 加上版本与源码地图等新工具（实现后再跑） | 完整流程 | 新工具的贡献 |

- A0 和 A1 只跑 v1 那一轨，是为了节省成本：方案 A 就相当于原来的单轨流程，足以衡量 MCP 的贡献。
- 每个实验臂先跑一次。如果几个实验臂之间差距不明显，再增加次数。

### 5.5 评分

- 从最终答案和中间答案中拆出一张核对清单，覆盖以下几类：
  - 目标；
  - 关键机制：显示交接、进入与唤醒、功耗测量、SRAM 布局；
  - 带章节出处的 HW 事实；
  - 已知限制；
  - 各模块的改动清单：TF-A、U-Boot、kernel、CM33 固件、用户态；
  - 风险；
  - 资料缺口；
  - 待决定事项。

  每一项都标明属于 5.2 中的哪一类。这张清单**需要用户复核**，因为答案是用户当年对齐并交付的。
- **召回率**：回放的产物覆盖了清单中多少项（覆盖、部分覆盖、缺失）。
- **错误**：用 `read_pages` 逐条核对回放产物所引用的页码，统计引用不实或事实错误的条目。
- **额外发现**：答案中没有、但回放找到的内容单独列出，由用户判断对错和价值。
- 同时记录每次运行的耗时和调用了哪些工具。
- **两轨流程值不值得**：在 A2 中对方案 A、方案 B 和融合版 M 分别评分。
  - M 要比 A 和 B 都好，两轨流程才算值得；
  - 同时核查 M 有没有丢掉 A 或 B 里命中答案的条目，也就是融合有没有丢分；
  - 再对照两轨多花的成本：Agent 数量、耗时、token。

### 5.6 准备工作

按依赖关系排序。A0 和 A1 用不到索引器的改动，所以索引器排在后面，和前面的工作并行。

1. 构造沙箱（按白名单），并通过 5.3 的两项隔离检查，同时确认 Claude Code 在沙箱里能登录、能连上实验用的 MCP 实例、能看到用户的 skill。
2. 从最终答案和中间答案中拆出核对清单，交给用户复核。和第 1 项并行。清单放在 rz-mcp 里，沙箱看不到。
3. 准备设计模板、设计 Agent 的说明，以及比较 Agent 的说明（按 3.3 的判断标准写）。模板只能使用通用结构，并且要通过金丝雀检索。
4. 用户在沙箱里**新开一个会话**，完成第 1 步，得到 v1 和 v2 后冻结。不能沿用写这份计划的会话，因为那个会话已经看过答案。
5. 跑 A0 和 A1。
6. 索引器支持 markdown 和层源码（目前只支持 PDF 和 BOM），并能为按截止日期建的库指定 git rev。之后跑 A2；A3 等新工具实现后再跑。
7. 第一期结束后，再决定是否进入第二期（第 5 步）。

## 6. 之后：回放 Tesera

Tesera 是 RZ/G2L 客户板的 bring-up（`MACHINE=rzg2l-tlc-01`，VLP v4.0.2，内核 6.1），有自己独立的 Yocto 项目。
回放它需要临时加入 G2L 的文档，属于范围例外，届时再做决定。

它代表另一类工作流：从原理图出发，修改 DTS、TF-A 的 DDR 配置和 U-Boot。
答案是 6 个补丁的 patch-set 和 README，可以和回放产物直接 diff 比较。

## 7. 搭建计划

### 7.1 跳过实验的代价，以及替代的验证方式

跳过实验会少掉三项证据：

- MCP 各个组成部分（文档查询、过去 PoC 库、源码地图）比"让 Claude Code 直接读 PDF 和源码"到底强多少。这正是《探索阶段工作计划》第 1 节提出的关键问题"值不值得做"，目前仍然没有证据。如果要向团队说明 MCP 的价值，这里是一个缺口。
- 两轨加融合是否值得它多花的成本，大约是单轨的 3 倍 Agent 开销。
- 以后改进工具时，没有可以对比的基线。

替代的验证方式分两层：

- **组件级已知答案测试**：每个组件都用现有的材料验证，不需要沙箱（见 7.2 的"验证"一列）。
- **第一个真实 PoC 作为验收**，记录以下几项：
  - 第 1 步和第 4 步对齐时，用户纠正了几处、属于什么类型；
  - 第 4 步发现了几处事实错误；
  - 返工的次数，以及每一步的耗时；
  - 第 6 步上板的结果，以及交付包能否在洁净环境中复现。

### 7.2 搭建顺序

| 阶段 | 项目 | 内容 | 验证（已知答案） |
|---|---|---|---|
| B0 | 版本管理 | 把 rz-mcp 纳入本地 git 管理（目前不是 git 仓库），这样之后修改服务器代码都有记录 | — |
| B1 第 1～4 步 | B1a 流程骨架 | `poc-new`：在 Yocto 项目主目录下创建 PoC 仓库，目录沿用 lowpower-display 的样式，另加 `design/` 存放 00～01 的文件。设计模板。两个项目级子 Agent：设计用的 `poc-designer`、比较融合用的 `poc-merger`。MCP prompt `/rz-docs:poc`，负责提示用户在哪一步手动输入哪个 skill | 用 `poc-new` 生成的目录结构和 rzg3l-lowpower-display 一致；`/rz-docs:poc` 在 VS Code 面板中能出现并正常传入参数 |
| | B1b 过去 PoC 库 | 把三个 G3L PoC 收录进 MCP：markdown 文档、各层的 README、补丁的标题和说明。现有 schema 的 chunk 用 `locator` 定位，已经支持没有页码的条目 | 检索 DISP_RET，能命中 lowpower 指南的 §3.3；检索 RIIC0 和音频，能命中已知限制 |
| | B1c 版本与源码地图 | `get_bsp_release`；从 DL_DIR 的 git 镜像读取官方 rev 的源码；`patch_stack` | 返回今天已核实的 pin 和 rev；kernel-source 在官方 rev 之上正好是 20 个提交 |
| B2 第 5 步 | 构建与交付 | `poc-use`，用于切换配置；容器内构建；`RM_WORK_EXCLUDE`；`package`；洁净室复现 | 用 `poc-use lowpower-display` 重新生成的配置，和当前 build/conf 中的一致；用 `package` 打出的结构和 rzg3l-lowpower-display 一致 |
| B3 第 6 步 | 上板 | 安装 TFTP 和 NFS；串口 harness；板子准备清单；`locate_log` | 第一个真实 PoC 上板时验证 |

进度（2026-11-25）：

- **B0 完成**：rz-mcp 已纳入本地 git 管理。不跟踪 `docs/`（872 MB 的原始资料）和 `data/`（生成的库、调用日志和 token）。
- **B1a 完成**：包括 `workflow/`（`poc-new`、`install-agents`、模板、两个子 Agent），以及 MCP prompt `poc`（`src/rzmcp/workflow.py`）。
  - 已验证：
    - 31 项已知答案测试全部通过：`poc-new` 生成的目录结构、指南文件名、指南的 `##` 标题、`md2pdf.py`、`.gitignore` 都和 rzg3l-lowpower-display 一致；prompt 在每个阶段给出的下一步都正确；install-agents 正常工作；子 Agent 的 frontmatter 正确；
    - 通过 HTTP 对运行中的服务做了端到端测试：prompt 能列出，参数只有 `name`；对已交付的 rzg3l-lowpower-display 会提示"停下，不要写入"；原有工具不受影响。
  - 还没有验证：
    - `/rz-docs:poc` 在 VS Code 面板里能否显示和使用；
    - 在 rzg3l-bsp-v1 下的会话能否调起两个子 Agent。

  这两项要在第一次实际使用时由用户确认。

默认的存放位置如下，需要时可以改：

- 脚本和模板放在 rz-mcp 的 `workflow/` 下，由 MCP 以 prompts 和 resources 的形式提供；
- 子 Agent 的定义放在 `rzg3l-bsp-v1/.claude/agents/`，只在 Yocto 项目的会话中生效；
- Tesera 属于 G2L，超出范围，暂不入库。

### 7.3 B3 的网络：USB 网卡直连

**已核实（2026-11-25）**

- **网卡**：`enx207bd2aa6c7a`，ASIX AX88179 千兆网卡（驱动 `cdc_ncm`），MAC `20:7b:d2:aa:6c:7a`。系统已识别；没接板子时没有 carrier。
- **主机的现状**：
  - 只有 `enp1s0` 接公司内网，地址 10.166.35.229/22，默认路由走它；Docker 用的是 172.17.0.0/16；
  - NetworkManager 在运行；
  - 已装了 `dnsmasq-base`（NetworkManager 用的），没装 dnsmasq 服务和 NFS 服务；
  - sudo 需要密码，所以安装和配置要由用户执行。
- **官方 BSP 支持网络启动**：
  - U-Boot：`smarc-rzg3l_defconfig`（@`16e6324c`）里开着 `DWC_ETH_QOS`（GBETH）、`RENESAS_RAVB` 和 `CMD_DHCP`；
  - 内核：官方 defconfig（@`e6c894b7`）里开着 `STMMAC_ETH`、`DWMAC_RENESAS_GBETH`、`IP_PNP_DHCP`、`NFS_FS` 和 `ROOT_NFS`。
- **例外**：rzg3l-fast-boot-seamless-logo 的 `fastboot.cfg` 为了缩短启动时间，关掉了 `STMMAC_ETH`。用了这个层的 PoC，Linux 里没有以太网，因此不能用 NFS root。这种情况下 U-Boot 的 TFTP 仍然可以用；rootfs 改走 SD 卡，或者加一个只在开发时使用的配置片段，重新打开以太网。这个片段不放进交付物。

**进度（2026-11-25）**：B3 中"写 microSD"这一部分已经做好（`workflow/board/`）。
- 方式：板子进入 U-Boot 后，按 bmap 分块用 TFTP 下载，每块先校验 CRC32，再 `mmc write`，最后全部读回校验。只写有数据的区段，和 bmaptool 一样。
- 离线测试 12 项全部通过（`eval/test_board.py`，对着假 U-Boot 运行）。其中，今天构建的 lpdemo 镜像（562 MB 有效数据）写入后，13 个区段的 sha256 都和 bmap 一致。
- 还没在实机上运行：需要用户先执行 `setup-host.sh`，并接好板子。

**方案**（B3 时实施）

| 项目 | 设置 |
|---|---|
| 地址 | 主机这一侧固定为 192.168.50.1/24，EVK 由 DHCP 分配 192.168.50.100～150。不设网关，也不做 NAT：EVK 和公司网络互相不通。如果公司内网用到了 192.168.50.0/24，就换一个网段 |
| 网卡配置 | 用 NetworkManager 为这块网卡建一个固定 IP 的配置：不作为默认路由，也不提供 DNS |
| DHCP 和 TFTP | 用 dnsmasq 一个服务同时提供，只绑定这块网卡（`interface=` 加 `bind-dynamic`，插上网卡、有了地址才开始监听），`port=0` 不提供 DNS，以免和 systemd-resolved 冲突。TFTP 的根目录是 `/srv/tftp` |
| NFS | 用 nfs-kernel-server。`/srv/nfs/<PoC>` 只开放给 192.168.50.0/24（rootfs 需要 `no_root_squash`） |
| 防火墙 | ufw 如果是开启状态，只在这块网卡上放行 DHCP、TFTP 和 NFS |
| 交付方式 | 提供一个可以重复执行、带撤销功能的脚本，由用户用 sudo 运行 |
| 验证 | 在网卡有 carrier 时（接上 EVK 并上电）确认：各服务只在 192.168.50.1 上监听；EVK 的 U-Boot 能通过 `dhcp` 和 `tftpboot` 取得内核；Linux 能以 NFS root 启动 |
