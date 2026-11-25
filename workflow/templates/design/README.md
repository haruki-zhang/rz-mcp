# design/：第 1～4 步的工作文件

本目录只在内部使用，第 5 步打包交付物时会排除。流程定义见 rz-mcp 的 `plan/PoC_Workflow_Plan.md` 第 3 节。

每一步都通过 `/rz-docs:poc <名称>` 推进：命令会检查本目录，提示当前处在哪一步、下一步该做什么。
文件第一行如果还带着 `<!-- TEMPLATE:` 标记，就表示这个文件还没有填写。

| 文件 | 步骤 | 谁写 | 内容 |
|---|---|---|---|
| `00-request.md` | 1 | 主会话 | 需求原文，逐字保存 |
| `00a-restated.md` | 1 | 主会话 | 用户手动输入 `/in-your-own-words` 得到的复述、用户的纠正，以及对齐后的 **v1** |
| `00b-sharpened.md` | 1 | 主会话 | 用户手动输入 `/sharpen` 得到的四项分析，以及改写后的 **v2** |
| `research-a.md`、`research-b.md` | 2 | `poc-designer` | 两轨各自的调研笔记，每条事实都带出处 |
| `01a-design-v1.md`、`01b-design-v2.md` | 3 | `poc-designer` | 两轨各自写的方案，按 `design-template.md` 的结构 |
| `proto-a/`、`proto-b/` | 3 | `poc-designer` | 主机原型。两轨各用各的目录 |
| `01-design.md` | 3 | `poc-merger` | 融合版方案 |
| `01-merge-notes.md` | 3 | `poc-merger` | 融合说明：每一项来自哪一版、事实核实的结果、舍弃了什么、冲突如何处理 |
| `02-decisions.md` | 4 | 主会话 | 用户对齐时作出的决定。对齐完成后打 tag `design-v1` |

关口：没有 tag `design-v1`，不进入第 5 步。
