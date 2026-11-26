# workflow/：RZ/G3L PoC 工作流

流程的定义见 `plan/PoC_Workflow_Plan.md` 第 3 节。本目录是流程工具的唯一来源，已纳入 rz-mcp 的 git 管理。

| 文件 | 作用 |
|---|---|
| `poc-new` | 在 Yocto 项目主目录下创建 PoC 仓库。目录结构沿用 rzg3l-lowpower-display，另加 `design/` |
| `install-agents` | 把 `agents/*.md` 复制到 Yocto 项目的 `.claude/agents/`；加 `--check` 只做比较，不复制 |
| `agents/poc-designer.md` | 第 2、3 步的单轨设计 Agent（A 轨或 B 轨），两轨相互独立 |
| `agents/poc-merger.md` | 第 3 步的比较融合 Agent |
| `templates/poc/` | 交付物骨架：README、指南、image/README、md2pdf.py |
| `templates/design/` | 第 1～4 步的文件模板，包括设计模板 `design-template.md` |
| `build/` | 第 0 步：`yocto-project`（判断、搭建或复用 Yocto 项目，安装可选组件）。第 5 步：`poc-use`、`poc-build`、`poc-env`（在容器里运行 devtool 等命令）、`poc-package`。见 [build/README.md](build/README.md) |
| `board/` | 第 6 步（以及之前的上板实验）：`evk-flash`（写 bootloader 和 rootfs）、`evk-verify`（执行 `design/verify.toml`，生成 `design/03-verification.md`）、`evk-watch`（Agent 在后台运行上板命令时，用它把需要人操作的提示转告用户），见 [board/README.md](board/README.md) |

Yocto 项目默认是与 rz-mcp 同级的 `rzg3l-bsp-v1/`，所有 PoC 共用它。某个 PoC 也可以用另一个项目：在 `/rz-docs:poc` 的第二个参数里写目录（见下文）。脚本用 `--project DIR` 指定，环境变量 `RZ_POC_PROJECT` 可以改默认值。

## 使用方法

1. 在 Yocto 项目目录（`rzg3l-bsp-v1/`）下打开 Claude Code。子 Agent 是项目级的，只有在这个目录下的会话才能用到。
2. 输入 `/rz-docs:poc <名称> [目录]`，例如 `/rz-docs:poc lp-anim`。名称只能是一个词，因为 Claude Code 按空格拆分参数；设想原文按提示粘贴到对话里。
   - 不写目录：用上次为这个 PoC 指定的目录，没有指定过就用共用的 `rzg3l-bsp-v1`。
   - 写了目录（绝对路径，必须在构建容器挂载的 `/home/mpu-epmaj/max` 下）：目录不存在或为空时，先进入第 0 步，由 `yocto-project setup` 自动搭建一个独立的项目，搭建期间可以先做第 1 步；已经是可用的项目时直接复用；既不为空、又不是可用项目的目录，流程会停下，不做任何改动。目录会被记下，之后不用再写。
   - 子 Agent 是项目级的：用新项目时，到第 2 步要在那个目录下重新打开 Claude Code，再输入 `/rz-docs:poc <名称>`。
3. 按提示操作。命令都由 Agent 运行，包括构建和上板；你只做拨码、上电、RESET、插拔卡这类物理操作，以及少数需要 sudo 的命令。每完成一步，再输入一次 `/rz-docs:poc <名称>`，它会读取磁盘上的状态，告诉你下一步做什么。`/in-your-own-words` 和 `/sharpen` 两个 skill 由你手动输入。
4. 第 4 步对齐完成后，PoC 仓库会打上 tag `design-v1`。之后同一个命令继续引导第 5、6 步，顺序是：实现 → `poc-use` 和 `poc-build` → `evk-flash` → `evk-verify` → `poc-package` → tag `release-v1`。每一步是否完成，都从磁盘上判断：
   - 构建：要有该 PoC 配置下的构建报告，结果 OK，并且晚于最后一次提交；
   - 写板子：写入的必须是这次构建出来的镜像；
   - 验证：`03-verification.md` 要么全部通过，要么写明了"用户已确认接受"；
   - 交付：`dist/` 下已经有交付包。

修改 `agents/` 之后，运行 `python3 workflow/install-agents` 重新安装（其他项目加 `--project <目录>`）。修改 prompt（`src/rzmcp/workflow.py`）之后，要重启服务：`systemctl --user restart rz-mcp`。
