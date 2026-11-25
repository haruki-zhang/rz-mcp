"""The `poc` MCP prompt: steps 1-4 of the RZ/G3L PoC workflow (plan/PoC_Workflow_Plan.md, section 3).

The prompt inspects the PoC repository on disk and tells the main session which step it is at and what to do
next. A design file counts as unfilled while its first line still carries the `<!-- TEMPLATE:` marker.
The two skills (/in-your-own-words, /sharpen) stay manual: the prompt asks the user to type them.
"""
import filecmp
import os
import re
import subprocess
from pathlib import Path

from . import ROOT

WORKFLOW = ROOT / "workflow"
MARKER = "<!-- TEMPLATE:"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# (file in design/, step, what it holds)
FILES = [
    ("00-request.md", "1", "需求原文"),
    ("00a-restated.md", "1", "v1（/in-your-own-words，已对齐）"),
    ("00b-sharpened.md", "1", "v2（/sharpen）"),
    ("01a-design-v1.md", "2-3", "方案 A（poc-designer，轨道 A）"),
    ("01b-design-v2.md", "2-3", "方案 B（poc-designer，轨道 B）"),
    ("01-design.md", "3", "融合版（poc-merger）"),
    ("01-merge-notes.md", "3", "融合说明（poc-merger）"),
    ("02-decisions.md", "4", "对齐记录"),
]

RULES = """\
## 始终遵守

- `/in-your-own-words` 和 `/sharpen` 只能由用户手动输入。不要代替它们，也不要自己写复述或深化来替代。
- 每完成一步，在 PoC 仓库里执行一次 `git commit`，提交说明里写明是第几步。
- 构建只能由主会话执行，同一时间只跑一个 bitbake（所有 PoC 共用一个构建目录）。执行前先告诉用户要构建什么、大约需要多久。
- 只针对 RZ/G3L。流程的定义见 `{plan}` 第 3 节。"""


def project_dir() -> Path:
    return Path(os.environ.get("RZ_POC_PROJECT", ROOT.parent / "rzg3l-bsp-v1"))


def repo_name(name: str) -> str:
    return name if name.startswith("rzg3l-") else f"rzg3l-{name}"


def filled(p: Path) -> bool:
    if not p.exists():
        return False
    with open(p, encoding="utf-8") as f:
        return not f.readline().startswith(MARKER)


def has_tag(repo: Path, tag: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "tag", "-l", tag], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == tag


def agents_current(project: Path) -> bool:
    for src in (WORKFLOW / "agents").glob("*.md"):
        out = project / ".claude" / "agents" / src.name
        if not out.exists() or not filecmp.cmp(src, out, shallow=False):
            return False
    return True


def next_step(name: str, repo: Path, project: Path, done: dict[str, bool]) -> str:
    d = repo / "design"
    if not repo.exists():
        return f"""\
## 下一步：创建 PoC 仓库（第 1 步）

1. 先问用户指南标题的写法。它决定交付文档的文件名，例如 "LowPower Display" 会生成 `RZG3L-LowPower-Display-Guide.md`。
2. 运行 `python3 {WORKFLOW}/poc-new {name} --title "<标题>"`，这会创建 `{repo}`。
3. 请用户粘贴设想的原文，有附件的话也请提供路径。原文**逐字**写入 `{d}/00-request.md`，不要改写，然后删掉第一行的标记；附件复制到 `{d}/attachments/`。
4. 提交，然后请用户**手动输入 `/in-your-own-words`**。"""
    if not d.is_dir():
        return f"""\
## 停下：`{repo}` 已经存在，但不是用 poc-new 创建的（没有 `design/`）

这很可能是一个已经交付的仓库，不要往里面写东西。请用户另起一个名称；如果确实要在这个仓库里补建 `design/`，先请用户明确同意。"""
    if not done["00-request.md"]:
        return f"""\
## 下一步：保存需求原文（第 1 步）

请用户粘贴设想的原文，有附件的话也请提供路径。原文**逐字**写入 `{d}/00-request.md`，不要改写，然后删掉第一行的标记；附件复制到 `{d}/attachments/`。
提交，然后请用户**手动输入 `/in-your-own-words`**。"""
    if not done["00a-restated.md"]:
        return f"""\
## 下一步：复述与对齐，得到 v1（第 1 步）

- 如果对话中还没有 `/in-your-own-words` 的复述：请用户**手动输入 `/in-your-own-words`**，到此为止，等用户输入。
- 如果已经有复述：请用户确认，或指出要纠正的地方，反复进行，直到用户明确确认。
- 用户确认后：在 `{d}/00a-restated.md` 中写入复述、用户的每一处纠正（原样记录）和对齐后的 v1，删掉标记，提交。然后请用户**手动输入 `/sharpen`**。"""
    if not done["00b-sharpened.md"]:
        return f"""\
## 下一步：深化，得到 v2（第 1 步）

- 如果对话中还没有针对 v1 的 `/sharpen` 结果：请用户**手动输入 `/sharpen`**，到此为止，等用户输入。
- 如果已经有结果：把四项分析和改写后的 v2 原样写入 `{d}/00b-sharpened.md`，删掉标记，提交。v2 不单独对齐。
- 然后再次运行 `/rz-docs:poc {name}`，进入第 2、3 步。"""
    tracks = [t for t, f in (("A", "01a-design-v1.md"), ("B", "01b-design-v2.md")) if not done[f]]
    if tracks:
        agents = ("" if agents_current(project) else
                  f"0. 子 Agent 的定义没有安装，或者不是最新版：先运行 `python3 {WORKFLOW}/install-agents`。\n")
        which = "两个" if len(tracks) == 2 else "一个"
        return f"""\
## 下一步：两轨独立设计（第 2、3 步）

{agents}1. 启动{which} `poc-designer` 子 Agent，负责轨道 {"、".join(tracks)}。有两个的话，在同一条消息里同时启动，让它们并行运行。
   委托消息里**只写**这三项：PoC 路径 `{repo}`、Yocto 项目路径 `{project}`、轨道。不要写你自己的看法，也不要转述对话里的讨论，这样才能保证两轨相互独立。
2. 都返回后，把它们的构建请求合并在一起。需要构建的，先向用户确认，再在主会话里按顺序执行。构建结果交回对应的 Agent，让它更新自己的方案。
3. 提交 `design/`，然后再次运行 `/rz-docs:poc {name}`。"""
    if not (done["01-design.md"] and done["01-merge-notes.md"]):
        return f"""\
## 下一步：比较与融合（第 3 步）

1. 启动一个 `poc-merger` 子 Agent。委托消息里**只写** PoC 路径 `{repo}` 和 Yocto 项目路径 `{project}`。
2. 它返回后，如果有构建请求，按第 2、3 步的方法处理。然后提交 `design/`，再次运行 `/rz-docs:poc {name}`，进入对齐。"""
    if not done["02-decisions.md"] or not has_tag(repo, "design-v1"):
        return f"""\
## 下一步：用户对齐（第 4 步）

1. 向用户呈现以下内容：
   - 融合版 `01-design.md` 的第 0 节结论；
   - 融合说明 `01-merge-notes.md` 第 1 节的核实统计；
   - 融合版第 9 节的每个待决定事项；
   - 融合说明第 6 节的每个可选项。

   待决定事项和可选项要**逐项**请用户决定。选项明确的，可以用提问工具。
2. 把用户的每个决定和修改记入 `{d}/02-decisions.md`，并同步修改 `01-design.md`。
3. 用户**明确确认**后：勾选结论，删掉标记，提交，然后在 PoC 仓库打 tag `design-v1`。没有用户的明确确认，不要打 tag。"""
    return f"""\
## 第 1～4 步已完成（tag `design-v1`）

第 5 步的工具（`poc-use`、`package` 等，计划中的 B2）还没有就绪。在此之前，可以按 `01-design.md` 第 10 节的步骤手动推进。"""


def poc_prompt(name: str) -> str:
    name = name.strip()
    if not NAME_RE.match(name):
        return (f"PoC 名称 {name!r} 不合法：请用小写单词加 `-` 连接，例如 `lowpower-display`。"
                "用法：`/rz-docs:poc <名称>`")
    project = project_dir()
    repo = project / repo_name(name)
    done = {f: filled(repo / "design" / f) for f, _, _ in FILES}
    rows = "\n".join(f"| {step} | `design/{f}` | {what} | {'✓' if done[f] else '—'} |" for f, step, what in FILES)
    tag = "✓" if repo.exists() and has_tag(repo, "design-v1") else "—"
    state = (f"| 步骤 | 文件 | 内容 | 状态 |\n|---|---|---|---|\n{rows}\n| 4 | tag `design-v1` | 对齐完成 | {tag} |"
             if repo.exists() else "仓库还不存在。")
    return f"""\
# RZ/G3L PoC 工作流：{name}（第 1～4 步）

你是主会话，负责推进这个流程。下面是从磁盘上读到的当前状态，以及下一步要做的事。
每完成一步，用户都可以再次输入 `/rz-docs:poc {name}` 继续。

- PoC 仓库：`{repo}`
- Yocto 项目：`{project}`

{state}

{next_step(name, repo, project, done)}

{RULES.format(plan=ROOT / "plan" / "PoC_Workflow_Plan.md")}
"""
