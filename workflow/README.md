# workflow/：RZ/G3L PoC 工作流（第 1～4 步）

流程的定义见 `plan/PoC_Workflow_Plan.md` 第 3 节。本目录是流程工具的唯一来源，已纳入 rz-mcp 的 git 管理。

| 文件 | 作用 |
|---|---|
| `poc-new` | 在 Yocto 项目主目录下创建 PoC 仓库。目录结构沿用 rzg3l-lowpower-display，另加 `design/` |
| `install-agents` | 把 `agents/*.md` 复制到 Yocto 项目的 `.claude/agents/`；加 `--check` 只做比较，不复制 |
| `agents/poc-designer.md` | 第 2、3 步的单轨设计 Agent（A 轨或 B 轨），两轨相互独立 |
| `agents/poc-merger.md` | 第 3 步的比较融合 Agent |
| `templates/poc/` | 交付物骨架：README、指南、image/README、md2pdf.py |
| `templates/design/` | 第 1～4 步的文件模板，包括设计模板 `design-template.md` |

Yocto 项目默认是与 rz-mcp 同级的 `rzg3l-bsp-v1/`，可以用环境变量 `RZ_POC_PROJECT` 指定其他目录。

## 使用方法

1. 在 Yocto 项目目录（`rzg3l-bsp-v1/`）下打开 Claude Code。子 Agent 是项目级的，只有在这个目录下的会话才能用到。
2. 输入 `/rz-docs:poc <名称>`，例如 `/rz-docs:poc lp-anim`。名称只能是一个词，因为 Claude Code 按空格拆分参数；设想原文按提示粘贴到对话里。
3. 按提示操作。每完成一步，再输入一次 `/rz-docs:poc <名称>`，它会读取磁盘上的状态，告诉你下一步做什么。`/in-your-own-words` 和 `/sharpen` 两个 skill 由你手动输入。
4. 第 4 步对齐完成后，PoC 仓库会打上 tag `design-v1`。第 5 步的工具（计划中的 B2）还在开发中。

修改 `agents/` 之后，运行 `python3 workflow/install-agents` 重新安装。修改 prompt（`src/rzmcp/workflow.py`）之后，要重启服务：`systemctl --user restart rz-mcp`。
