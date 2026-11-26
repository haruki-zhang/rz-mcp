# rz-docs MCP 服务器：下一步改什么（基于 evk-mac 运行记录的缺口分析）

> 2026-11-26。原始材料是第一个真实 PoC（evk-mac）的完整运行记录。脚本都在 `eval/gap/`，可以重跑（见第 7 节）。
> 原型都放在 `eval/gap/proto/`，**没有接入服务器**。

## 0. 结论

- **文档检索不是瓶颈。**
  - 用已核实事实生成查询：在已收录的文档上，前 8 条命中 15/17；
  - Agent 自己发出的检索，之后打开的那一页大多就在结果里；
  - MCP 返回的 22 次空结果，基本都是如实的否定回答（例如"没有 EEPROM"），之后 Agent 都接着用 MCP 继续查。
- **Agent 离开 MCP，都是因为要读的东西不在 MCP 的范围里。** 人工核对后，一共有 97 次绕道（Bash 读取），按次数排序：
  1. BSP 层的元数据，以及其他组件的官方源码：40 次；
  2. 过去的 PoC 和过去的上板日志：28 次；
  3. 为主机原型找工具链：16 次；
  4. 构建结果、构建配置和状态：11 次；
  5. 未收录的 Start-up Guide：2 次。
- **事实出处**：融合时核实的 88 条事实里，有 18 条（20%）的出处只能通过绕道拿到。
- **三个原型的测量结果**：

  | 原型 | 覆盖的绕道 | 内容是否一致 | 能变成 MCP 出处的事实 |
  |---|---|---|---|
  | 原型 1：层元数据和其他组件的源码 | 40 次中的 35 次全部覆盖 | 128/128 个文件一致 | 8 条 |
  | 原型 2：过去的 PoC 和日志（只读，带出处） | 28 次中约 24 次 | 57/57 个文件一致 | 9 条 |
  | 原型 3：收录 Start-up Guide | — | — | 1 条：F-20 引用的 6 页全部可检索，唯一一条"无法核实"的文档事实因此可以核实；原有检索不受影响 |

- **推荐顺序**：
  1. 原型 3：成本最低，一条 manifest 记录；
  2. 原型 1：绕道最多。和正在进行的 source snapshot 工作合并，见第 5 节；
  3. 原型 2：需要你决定是否解除 B1b 的暂缓。
- 工具链的问题属于工作流的环境，不属于 MCP，单独处理。

## 1. 原始材料和方法

| 材料 | 规模 |
|---|---|
| evk-mac 主会话和 3 个子 Agent 的运行记录（`~/.claude/projects/-home-mpu-epmaj-max-workspace-rzg3l-bsp/f893911b…`） | 948 次工具调用；其中 MCP 426 次，Bash 359 次 |
| `data/calls.jsonl`（服务器的调用日志） | 554 次；`bsp_source` 加 `bsp_grep` 共 323 次 |
| `rzg3l-evk-mac/design/01-merge-notes.md` 第 1 节 | 88 条事实：85 条通过、1 条不通过、2 条无法核实，每条都写了出处 |

**方法**：

1. 提取每一次工具调用，包括参数和返回内容（`extract.py`）。
2. 给每次 MCP 调用标记结果：是否为空，空结果之后 Agent 做了什么。给每次 Bash 调用标记用途（`classify.py`）。分类规则写成正则，再逐条人工阅读，误判的在代码里写明覆盖规则。只有"读取某个 MCP 工具本可以回答的内容"才算绕道；运行原型、读写 PoC 自己的文件、构建和上板都不算。
3. 把 merge-notes 解析成已知答案集（`facts.py`），按出处类型拆开。文档类的出处用来做检索评测（`retrieval.py`）。
4. 原型都用重放来测：把运行记录里真实的绕道命令逐条解析（跟踪 `cd`、变量和通配符），找出它读的每一个目标，再检查原型能不能返回相同的内容（`replay_layers.py`、`replay_past.py`、`sug_eval.py`）。

## 2. 缺口表

| 类别 | 绕道次数（主会话 / 融合 / 设计 A+B） | 依赖它的事实 | 原始例子 | 候选改进 |
|---|---|---|---|---|
| BSP 层元数据：配方、machine 和 distro 的 conf、wks、inc | 32（6 / 5 / 21） | 6（F-21、F-22、F-69～F-71 等） | `cat meta-renesas/meta-rz-bsp/conf/machine/smarc-rzg3l.conf`；`find … rz-image-bootpart-esd.wks* \| xargs cat`；`sed -n 300,380p poky/…/systemd_255.21.bb` | 按发布的 pin 读取层文件，并能检索（原型 1）。其中 10 次只是在找变量的值，`bitbake -e` 能直接回答 |
| 其他组件的官方源码（systemd） | 8（1 / 1 / 6） | 4（F-72～F-74 等） | `git --git-dir=…/git2/github.com.systemd.systemd-stable.git show 70500d3…:network/99-default.link` | 根据配方解析出 SRCREV 和镜像，读取源码（原型 1 的 `recipe_source`） |
| 过去的 PoC 和过去的上板日志 | 28（7 / 3 / 18） | 9（F-39、F-42、F-62、F-76～F-80 等） | `grep -rIl -i "ethaddr\|local-mac\|…" rzg3l-bsp-v1`；`sed -n 180,210p RZG3L-Fast-Boot-Seamless-Logo-Guide.md`；`grep -n "device MAC address" logs/*.log` | 只读访问、带出处（原型 2），或者完整的 B1b |
| 为主机原型找工具链（交叉 gcc、sysroot、qemu-aarch64、dtc） | 16（0 / 0 / 16，全部是设计 B） | — | `ls …/sysroots-components/x86_64/gcc-cross-aarch64/usr/bin`；`find …/recipe-sysroot` | **属于工作流的环境**：告诉设计 Agent 去哪里拿交叉工具链和 qemu-user，或者提供一个脚本 |
| 构建结果：work 目录、`.config`、编译日志 | 6（4 / 0 / 2） | — | `grep warning: …/u-boot/…/temp/log.do_compile`；`ls …/work/*/u-boot*` | snapshot 工作里的 `build_facts`（见第 5 节） |
| 构建配置和状态 | 5（2 / 1 / 2） | — | `cat build/conf/poc-use.json bblayers.conf`；`grep DL_DIR local.conf` | `check_build_env` 已经能回答，但 Agent 没有用：在说明里强调一下即可 |
| 未收录的 PDF（Start-up Guide） | 2（0 / 0 / 2） | 1（F-20，无法核实，引用了 6 页） | `pdftotext -layout "…/r01us0858ej0100-rzg3l(Linux Start-up_Guide_RZG3L)_rc1.pdf"` | 收录进索引（原型 3） |

**不是缺口的部分**（有证据，暂时不动）：

- **文档检索的排序**：见第 3 节。
- **空结果**：22 次（`bsp_grep` 16、`search_docs` 4、`lookup_register` 2）都是如实的否定回答，之后都接着用 MCP 继续查。
- **`bsp_source` 的读法**：158 次读了 58 个路径，110 次只请求一个片段，典型做法是先 grep 再读附近 20～80 行。两条轨道按规定独立调研，所以同一个文件会被读好几次。
- **结构化寄存器**：只用了 18 次。OTP 不在已覆盖的模块里，51 次 `read_pages` 也把需要的内容都读到了。只凭这一个 PoC，看不出扩展寄存器覆盖面的需求。

## 3. 基线（尺子）

| 指标 | evk-mac 基线 | 测法 |
|---|---|---|
| A. 用已核实事实生成的查询，在已收录文档上的命中 | 第 1 条命中 9/17，前 3 条 13/17，前 8 条 15/17 | 查询由事实里的英文词按固定规则生成；命中是指结果落在引用的那一页上。没命中的 2 条：F-07 的描述全是中文；F-11 是那条"不通过"的事实 |
| B. Agent 自己的检索，之后打开的那一页是否在结果里 | 33 次检索，24 次之后打开了某一页，其中 16 次那一页在结果里 | 剩下 8 次大多是"查不到 EEPROM，于是去看别的页"，这是判定规则的噪声 |
| C. 每个 PoC 的绕道次数 | 97 次（主会话 20、融合 10、设计 A 27、设计 B 40） | `classify.py` 加人工核对 |
| D. 出处只能通过绕道拿到的事实 | 18/88（20%） | `facts.py` |
| 冒烟评测（W3） | 检索 17/20，寄存器 8/8 | `eval/run_smoke.py` |

## 4. 原型与测量

### 原型 1：BSP 层元数据和其他组件的源码（`proto/layers.py`）

- **做法**：
  - `layer_source` 和 `layer_grep` 读取发布的四个层，以及 `meta-rz-features`。git 仓库按发布的 pin 读取（`git show rev:path`），所以工作区里的改动不会被当成官方内容；tarball 的层按磁盘内容读，并注明这一点。
  - `recipe_source` 从配方（.bb 和它 require 的 .inc）里解析出 git 的 SRC_URI 和 SRCREV，再从 DL_DIR 的镜像里读源码。
- **重放 40 次绕道**：
  - 35 次的所有目标都能由原型提供；3 次部分能提供（另外还读了 `build/conf`，或者没法展开的 `$d` 循环）；2 次只是在 `downloads/` 里找 systemd 镜像，`recipe_source` 会自己找到，这一步就不需要了；
  - 读到的 128 个层文件，原型返回的内容全部和 Agent 当时读到的一致，而且带上了 `meta-renesas@4823845d3a9a:路径` 这样的出处；
  - 事实 F-21、F-22、F-69～F-74 的出处因此可以改由 MCP 提供。
- **两点注意**：
  - 32 次层元数据绕道里，10 次只是在找变量的值（`TFA_UBOOT`、`WKS_FILE`、`DISTRO_FEATURES` 等），`bitbake -e` 能直接回答；另外 22 次要读文件本身（wks 布局、`do_configure` 的代码、整个 machine conf）。两种方式要一起提供。
  - 设计阶段，新项目里还没有 systemd 的镜像，当时 Agent 是去 rzg3l-bsp-v1 的镜像里读的。新项目里有了，是因为后来 BR-0 构建时取回了。要在设计阶段就能用，有两个办法：搭建时多取回一些源码（这个项目全部下载完是 5.4 GB，第 0 步时是 4.3 GB），或者允许只读另一个项目里同一 rev 的镜像。

### 原型 2：过去的 PoC 和上板日志（`proto/past.py`）

- **做法**：登记 rzg3l-bsp-v1 里已交付的 PoC 仓库，以及 `logs/`。git 仓库按 HEAD 读取，带 `仓库@commit` 的出处；其他的按磁盘内容读。不建索引，相当于 B1b 的轻量版本。
- **重放 28 次绕道**：
  - 19 次全部能覆盖，3 次部分能覆盖；
  - 另有 6 次是在整个共用项目里 grep，其中 5 次的命中都落在已登记的来源里，改成"在所有已登记来源里 grep"能给出同样的答案；
  - 读到的 57 个文件全部一致，其中 25 个带 git commit 出处。
- 依赖它的事实有 9 条（F-39、F-42、F-59、F-62、F-76～F-80）。
- **它解决的问题**：Agent 用 Bash 也能读到这些内容。它解决的是出处，以及在别的项目、别的机器上能不能用，并不是"能不能读到"。

### 原型 3：收录 Linux Start-up Guide（`proto/sug.py`，建在临时数据库里）

- 这份指南是 R01US0858EJ0100 Rev.1.00-rc1，48 页、61 个书签。全文没有 CONFIDENTIAL 标记。现在它只放在 rzg3l-bsp-v1 的根目录下，rz-mcp 的 catalog 里没有登记，但 `workflow/board/media.py` 有大量值引用的就是它。
- **用设计 Agent 当时的搜索词测**（`sug_eval.py`）：
  - 8 条查询里有 6 条能从这份指南里找到答案；
  - F-20 引用的 6 页（p.26、27、37、39、41、42）全部能检索到；
  - `ethaddr` 和 `SYS_LSI_MODE` 在指南里没有命中。
- **原有检索不受影响**：
  - 事实评测从 15/17 变成 16/18，多出来的就是 F-20；第 1 条就命中的数量还是 9；
  - Agent 自己的检索结果不变；
  - 冒烟评测仍然是 17/20 和 8/8。

## 5. 和正在进行的 source snapshot 工作的关系

工作区里有一个**还没提交**的功能，不是本次分析做的：`src/rzmcp/snapshot.py`、`workflow/build/src-snapshot`，以及 `bsp.py` 和 `bsp_releases.toml` 的改动（17:22）。它要提供三样东西：按配方读取源码（`[sources]`），`bsp_recipe`（`bitbake -e` 的结果），`build_facts`（官方配置下的 `.config`、编译产物和设备树）。它覆盖的范围和缺口表的前两行、"构建结果"那一行有重叠。本次分析能给它提供三点依据：

1. **systemd 的源码要能读。** 现在 catalog 里把 systemd 放在 `sources_env_only`，也就是只提供变量。但这次 Agent 读 systemd 源码绕道了 8 次，4 条已核实的事实依赖它（`99-default.link`、`link-config.c`、`net_get_unique_predictable_data`）。
2. **只给变量不够。** 层元数据的绕道里，22/32 要读文件本身，比如 wks、class 里的函数、整个 conf。所以 `bsp_recipe` 之外，还需要能按 rev 读取层文件。
3. **可以把重放当成验收标准**：把 `replay_layers.py` 的检查从原型 1 换成 snapshot 的工具，看那 40 次绕道能覆盖多少，内容是否一致。

这项工作不是本次分析做的，所以我没有改动或提交它。

## 6. 推荐（按证据排序）

| 顺序 | 做什么 | 证据 | 成本 |
|---|---|---|---|
| 1 | 收录 Start-up Guide：把 PDF 放进 `docs/boards/rzg3l-evk/`，在 manifest 里登记 | 唯一一条因文档缺失而无法核实的事实，因此可以核实；6/8 的真实查询能找到答案；没有退化 | 一条记录，重建约 40 秒 |
| 2 | 层元数据和其他组件的源码：按 rev 读取层文件并能检索；按配方读源码；变量的值由 `bitbake -e` 提供。和 snapshot 工作合并 | 40 次绕道，原型覆盖 35～38 次；128/128 一致；8 条事实 | 中等；和 snapshot 有重叠，先对齐再做 |
| 3 | 过去的 PoC：只读访问加出处（轻量）；或者完整的 B1b（建索引） | 28 次绕道，约 24 次可覆盖；57/57 一致；9 条事实 | 轻量版小；**是否解除 B1b 的暂缓由你决定** |
| 4 | （工作流，不属于 MCP）告诉设计 Agent 交叉工具链、qemu-user 和 dtc 在哪里，或者提供一个脚本 | 设计 B 为此绕道 16 次 | 小 |
| — | `check_build_env` 已经能回答构建配置的问题：在设计 Agent 的说明里点名它 | 5 次绕道 | 一行 |

## 7. 需要你决定的事项（实验回答不了）

1. **机密版硬件手册**（R01UH1152EJ0110）：要不要建索引，建了以后怎么限制使用。本次数据里没有看到对它的需求：GBETH 的寄存器信息，设计是从驱动源码里拿到的，没有出现"无法核实"。
2. **ticket support**：范围是什么，数据从哪里来。现在一条真实工单都没有，上面的结论都只来自 PoC。
3. **给其他机器或团队成员使用**：要不要打包、远程部署。现在的 prompt 和服务器都依赖本机的文件系统。
4. **过去 PoC 入库（B1b）是否解除暂缓**：证据见原型 2。轻量版已经能覆盖大部分需求，完整版多出来的是全文检索。
5. **是否扩展到 G2L/Tesera**。
6. Start-up Guide 用的是 rc1。如果你手上有正式版，换成正式版更好。这一点只影响收录哪个文件，不影响"要不要收录"的结论。

## 8. 局限

- 只有一个 PoC，而且主要改的是 U-Boot。所以层元数据和 U-Boot 源码的比重偏高，其他类型的 PoC（例如显示、多核）的分布可能不同。下一个 PoC 跑完之后，用同一组脚本再测一次。
- 绕道的分类是"正则加人工核对"。覆盖规则写在 `classify.py` 的 `OVERRIDES` 里，可以复查。
- 检索评测 A 的查询是按固定规则从事实描述里提取的，描述全是中文的事实（F-07）测不到。

## 9. 重跑

```sh
S=~/.claude/projects/-home-mpu-epmaj-max-workspace-rzg3l-bsp/f893911b-5d16-4c41-9405-236b852b749b.jsonl
python3 eval/gap/extract.py $S /tmp/gap/calls.jsonl
python3 eval/gap/classify.py /tmp/gap/calls.jsonl
python3 eval/gap/facts.py /home/mpu-epmaj/max/workspace/rzg3l-bsp/rzg3l-evk-mac/design/01-merge-notes.md --json /tmp/gap/facts.json
PYTHONPATH=src python3 eval/gap/retrieval.py /tmp/gap/facts.json /tmp/gap/calls.jsonl [--db X]
PYTHONPATH=src python3 eval/gap/replay_layers.py /tmp/gap/calls.jsonl
python3 eval/gap/replay_past.py /tmp/gap/calls.jsonl
PYTHONPATH=src python3 eval/gap/proto/sug.py /tmp/gap/rz-sug.db && PYTHONPATH=src python3 eval/gap/sug_eval.py /tmp/gap/rz-sug.db
```
