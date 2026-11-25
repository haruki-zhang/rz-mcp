"""The `poc` MCP prompt: steps 1-6 of the RZ/G3L PoC workflow (plan/PoC_Workflow_Plan.md, section 3).

The prompt inspects the PoC repository on disk and tells the main session which step it is at and what to do
next. A design file counts as unfilled while its first line still carries the `<!-- TEMPLATE:` marker.
The two skills (/in-your-own-words, /sharpen) stay manual: the prompt asks the user to type them.
"""
import datetime as dt
import filecmp
import json
import os
import re
import subprocess
from pathlib import Path

from . import ROOT

WORKFLOW = ROOT / "workflow"
BUILD = WORKFLOW / "build"
BOARD = WORKFLOW / "board"


def runs(kind: str) -> Path:
    return Path(os.environ.get(f"RZ_{kind.upper().replace('-', '_')}", ROOT / "data" / kind))
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
    return later_steps(name, repo, later_state(repo))


# ---------------------------------------------------------------- steps 5 and 6

def _ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso).timestamp()


def later_state(repo: Path) -> dict:
    """What steps 5-6 have produced, read from the PoC repository and the run reports."""
    s = {"layers": [d for d in (repo / "layers").glob("*") if (d / "conf" / "layer.conf").exists()]
         if (repo / "layers").is_dir() else []}
    bc = repo / "build-conf"
    s["build_conf"] = (bc / "layers").exists()
    s["image"] = (bc / "image").read_text().strip() if (bc / "image").exists() else None
    r = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%ct"], capture_output=True, text=True)
    s["last_commit"] = float(r.stdout.strip() or 0)
    s["build"] = s["flash"] = None
    for f in sorted(runs("build-runs").glob("*/report.json"), reverse=True):
        rep = json.loads(f.read_text())
        if isinstance(rep.get("conf"), dict) and rep["conf"].get("poc") == repo.name:
            rep["_path"] = str(f)
            s["build"] = rep
            break
    wic = None
    if s["build"] and s["build"]["result"] == "OK":
        wics = [p for p in s["build"]["produced"] if p.endswith(".wic.gz") and (not s["image"] or p.startswith(s["image"] + "-"))]
        wic = wics[-1] if wics else None
    s["wic"] = wic
    if s["build"] and s["build"]["result"] == "OK":
        t_build = _ts(s["build"]["start"])
        for f in sorted(runs("board-runs").glob("*/report.json"), reverse=True):
            rep = json.loads(f.read_text())
            if rep.get("result") == "OK" and _ts(rep["start"]) >= t_build and \
                    (rep["command"].startswith("bootloader") or any(w["file"] == wic for w in rep.get("written", []))):
                rep["_path"] = str(f)
                s["flash"] = rep
                break
    v = repo / "design" / "03-verification.md"
    text = v.read_text() if v.exists() else ""
    s["verified"] = bool(text) and ("结果：全部通过" in text or "用户已确认接受" in text) and \
        (s["flash"] is None or v.stat().st_mtime >= _ts(s["flash"]["start"]))
    s["verify_failed"] = bool(text) and not s["verified"]
    pk = sorted((repo / "dist").glob("*/SHA256SUMS")) if (repo / "dist").is_dir() else []
    s["package"] = pk[-1].parent if pk and v.exists() and pk[-1].stat().st_mtime >= v.stat().st_mtime else None
    s["release"] = has_tag(repo, "release-v1")
    return s


def later_steps(name: str, repo: Path, s: dict) -> str:
    built_ok = s["build"] and s["build"]["result"] == "OK" and _ts(s["build"]["start"]) >= s["last_commit"]
    if not (s["layers"] and s["build_conf"] and s["image"]):
        missing = [w for w, ok in (("layers/ 下的层", s["layers"]), ("build-conf/layers", s["build_conf"]),
                                   ("build-conf/image", s["image"])) if not ok]
        return f"""\
## 下一步：实现（第 5 步）

还缺：{"、".join(missing)}。按 `01-design.md` 第 5 节（改动清单）和第 10 节（PoC 步骤）实现。所有改动都放在本 PoC 仓库自己的层里，不要改 BSP，也不要改其他 PoC 的层。

1. **构建配置**
   - 如果本 PoC 基于当前的构建配置（例如已有的 combo），先运行 `python3 {BUILD}/poc-use --capture {repo}`，它会生成 `build-conf/layers` 和 `build-conf/local.conf`；
   - 在 `build-conf/layers` 的末尾加上本 PoC 的层，例如 `{repo.name}/layers/meta-rzg3l-<名称>`；
   - 把要构建的镜像名（例如 `lpdemo-image`）写进 `build-conf/image`，只写一行。
2. **建层**：`layers/meta-rzg3l-<名称>/conf/layer.conf`，`LAYERSERIES_COMPAT = "scarthgap"`，优先级要高于它所依赖的层。
3. **改动方法**
   - 改之前，先用 MCP 的 `patch_stack`、`bsp_source` 和 `check_build_env` 查清楚：改动会叠加在哪些补丁之上，涉及的配置在哪里关掉或打开；
   - 内核、U-Boot、TF-A 的源码：先运行 `python3 {BUILD}/poc-env devtool modify <配方>`，在 `build/workspace/sources/<配方>` 里修改并 `git commit`，再运行 `python3 {BUILD}/poc-env devtool finish <配方> ../{repo.name}/layers/meta-rzg3l-<名称>`；
   - 内核配置片段（`.cfg`）、设备树补丁、systemd 的设置等，通过 bbappend 加进来。
4. 提交，然后再次运行 `/rz-docs:poc {name}`。"""
    if not built_ok:
        prev = ""
        if s["build"] and s["build"]["result"] != "OK":
            prev = (f"\n上一次构建失败（`{s['build']['_path']}`）：{len(s['build']['errors'])} 条 ERROR。先读 report.json 里的 "
                    "errors 和同目录下的 bitbake.log，修正并提交之后再构建。\n")
        return f"""\
## 下一步：构建（第 5 步）
{prev}
1. 先告诉用户：要用 `{repo.name}` 的配置构建 `{s['image']}`，当前的 build/conf 会被替换（旧文件会自动备份到 `build/conf/backup/`）。
2. 运行 `python3 {BUILD}/poc-use {repo}`。
3. 在后台运行 `python3 {BUILD}/poc-build {s['image']}`。增量构建通常需要几分钟到几十分钟。
4. 构建成功后，再次运行 `/rz-docs:poc {name}`。

构建结果必须晚于本仓库的最后一次提交才算数：如果构建之后又改了代码并提交，要重新构建。"""
    if not s["wic"]:
        return f"""\
## 停下：构建报告 `{s['build']['_path']}` 里找不到 `{s['image']}` 的 `.wic.gz`

检查 `build-conf/image` 里写的镜像名，以及这个镜像的 wks 是否生成了 WIC，然后重新构建。"""
    if not s["flash"]:
        return f"""\
## 下一步：写板子（第 6 步）

构建产物：`{s['wic']}`（构建报告 `{s['build']['_path']}`）。它在 Yocto 项目的 `build/tmp/deploy/images/smarc-rzg3l/` 下。

1. 先运行 `/usr/bin/python3 {BOARD}/evk-flash doctor --image <deploy>/{s['wic']}`，有 FAIL 就先解决。
2. 选择写入方式：
   - 只改了内核或 rootfs：`evk-flash rootfs --image <deploy>/{s['wic']}`；
   - 改了 BL2、FIP 或 CM33 固件：`evk-flash emmc --image <deploy>/{s['wic']}`。
3. 在后台运行。看到 `>>> 需要你操作` 时，把内容原样转告用户（拨码、上电等）。
4. 写入成功后，再次运行 `/rz-docs:poc {name}`。"""
    if not s["verified"]:
        fail = ("\n上一次验证有未通过的项，见 `design/03-verification.md`。向用户逐项说明，按用户的决定处理："
                "要么修复（回到实现和构建），要么接受。接受时，在 `03-verification.md` 末尾写明"
                "\"用户已确认接受：<哪些项，以及理由>\"，然后提交。\n") if s["verify_failed"] else ""
        return f"""\
## 下一步：上板验证（第 6 步）
{fail}
写入记录：`{s['flash']['_path']}`。

1. 把 `01-design.md` 第 11 节写成 `design/verify.toml`，格式见该文件开头的说明。
   - 每一项都要写 `where` 和 `run`，以及 `expect`（或者 `boot` 类型的 `max_seconds`）；
   - 能在主机上客观判定的项，用 `where = "host"`，例如 ping 板子；
   - 写完删掉第一行的标记，然后提交。
2. 运行 `/usr/bin/python3 {BOARD}/evk-verify {repo}`。它会生成 `design/03-verification.md`，原始日志放在 `design/verify-logs/`。
3. 全部通过的话，提交，然后再次运行 `/rz-docs:poc {name}`。"""
    if not s["package"]:
        return f"""\
## 下一步：交付（第 5 步，打包）

1. 按 rzg3l-lowpower-display 的样式写好以下文件，内容以 `01-design.md`、`02-decisions.md` 和 `03-verification.md` 为准：
   - `README.md`；
   - `docs/<Guide>.md`，包括 Objective、How It Works、Implementation Steps、Running the Demo、Known Limitations、Deliverables 这几节。验证时接受了的限制，必须写进 Known Limitations；
   - 各层的 README，以及 `image/README.md`。
2. 运行 `python3 {BUILD}/poc-package {repo} --image <deploy>/{s['wic']} --version v1.0`。它会生成 PDF；如果还有模板占位没替换，会给出警告。
3. 请用户检查 `dist/` 里的交付物。用户确认之后，提交并打 tag `release-v1`。"""
    if not s["release"]:
        return f"""\
## 下一步：用户确认交付物

交付包：`{s['package']}`。请用户检查。确认后提交，并打 tag `release-v1`。"""
    return f"""\
## 全部完成（tag `release-v1`）

交付包：`{s['package']}`。如果要做洁净室复现，在正式交付前另行安排，具体做法见 `{BUILD}/README.md`。"""


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
    if repo.exists() and tag == "✓":
        s = later_state(repo)
        built = bool(s["build"] and s["build"]["result"] == "OK" and _ts(s["build"]["start"]) >= s["last_commit"])
        mark = lambda ok: "✓" if ok else "—"
        state += (f"\n| 5 | `layers/`、`build-conf/` | 实现 | {mark(s['layers'] and s['build_conf'] and s['image'])} |"
                  f"\n| 5 | `data/build-runs/` | 构建（晚于最后一次提交） | {mark(built)} |"
                  f"\n| 6 | `data/board-runs/` | 写板子（写入的是这次构建的镜像） | {mark(s['flash'])} |"
                  f"\n| 6 | `design/03-verification.md` | 上板验证 | {mark(s['verified'])} |"
                  f"\n| 5 | `dist/` | 交付包 | {mark(s['package'])} |"
                  f"\n| 5 | tag `release-v1` | 交付完成 | {mark(s['release'])} |")
    return f"""\
# RZ/G3L PoC 工作流：{name}（第 1～6 步）

你是主会话，负责推进这个流程。下面是从磁盘上读到的当前状态，以及下一步要做的事。
每完成一步，用户都可以再次输入 `/rz-docs:poc {name}` 继续。

- PoC 仓库：`{repo}`
- Yocto 项目：`{project}`

{state}

{next_step(name, repo, project, done)}

{RULES.format(plan=ROOT / "plan" / "PoC_Workflow_Plan.md")}
"""
