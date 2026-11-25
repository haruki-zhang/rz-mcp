# rz-mcp — RZ 文档 MCP 服务器（RZ/G3L 试点）

## 目录结构

```
catalog/manifest.toml   文档目录（唯一的元数据来源）：doc_id、Rev、类型、适用的 device/board、索引方式
docs/                   原始资料（只读），按归属分目录
  devices/<device>/       器件专属：HW UM、Datasheet、App Note
  boards/<board>/         板级：EBM、原理图、BOM、勘误、设计数据
  family/<family>/        跨器件：DRAM list、Linux 驱动手册
src/rzmcp/              索引器、查询层、MCP server
data/rz.db              生成物（可随时重建），另有 calls.jsonl（调用日志）、.token
eval/                   冒烟测试题和评测脚本
plan/                   计划书、可行性报告、W3 spike 报告
```

目录树只表达文件"归谁管"。一份文档对应多个 device、一块板服务多个 SoM 这类关系，记录在 manifest 里。
新增 device 的步骤：在 `docs/devices/<新device>/` 下放入文件，在 manifest 中登记，然后重建。

## 构建数据库

只需要 Python ≥3.11 和 poppler-utils，不需要第三方包：

```bash
.venv/bin/python -m rzmcp.build                # 全量重建 A 层和 B 层（约 16 秒）
.venv/bin/python -m rzmcp.build --registers    # 只重建 B 层，例如 manifest 里新增了 register_modules（约 1 秒）
.venv/bin/python -m rzmcp.build --restart      # 重建完成后重启常驻服务，让它加载新数据
.venv/bin/python eval/run_smoke.py             # 冒烟评测
.venv/bin/python -m rzmcp.cli lookup_register WDTSET_2   # 不经过 MCP，直接调用查询层
```

`rzmcp.build` 先在 `data/rz.db.tmp` 里构建，通过自检后再原子替换 `data/rz.db`；自检失败时不会改动现有的库。
正在运行的 server 会一直使用旧数据，直到被重启。
没有 `.venv` 时，也可以用系统自带的 `PYTHONPATH=src python3 -m rzmcp.build`，构建本身不依赖第三方包。

## 运行 MCP server（需要 `mcp` 包）

```bash
# 一次性准备环境（任选一种）
uv venv && uv pip install -e .                                  # 已安装 uv 时
python3 -m venv .venv && .venv/bin/pip install -e .              # 系统已安装 python3-venv / pip 时

# 本机模式（Streamable HTTP + Bearer token，只监听 127.0.0.1，这是默认值）
.venv/bin/python -m rzmcp.server --transport http --port 8765
# 局域网模式（只有在 MCP 白名单允许非 localhost 地址时才用）
.venv/bin/python -m rzmcp.server --transport http --host 0.0.0.0 --port 8765
```

首次启动时会生成 `data/.token`，也可以用环境变量 `RZ_MCP_TOKEN` 指定 token。

**在 Claude Code 中接入（当前方案：本机）：**

公司的 MCP 白名单只允许 localhost 地址，而且不允许 stdio 服务器，所以 Claude Code 和 server 必须运行在同一台电脑上。
从 Windows 使用时，在 VS Code 里通过 Remote-SSH 打开这台 Linux，此时 Claude Code 运行在 Linux 上。

```bash
claude mcp add --scope user --transport http rz-docs http://localhost:8765/mcp \
  --header "Authorization: Bearer $(cat data/.token)"
claude mcp list          # 应显示 rz-docs ... ✔ Connected
```

换 token 之后，先执行 `claude mcp remove rz-docs -s user`，再重新 add。

### 常驻运行（systemd 用户服务，不需要 sudo）

```bash
mkdir -p ~/.config/systemd/user && cp deploy/rz-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now rz-mcp   # 开启常驻
systemctl --user restart rz-mcp           # 修改代码或数据后重启
systemctl --user stop rz-mcp              # 临时停止，比如要手动另起一个实例调试时
systemctl --user disable --now rz-mcp     # 取消常驻
journalctl --user -u rz-mcp -f            # 查看日志
```

服务文件已经设好了低优先级和资源上限，给 Yocto 构建这类重任务让路。具体参数见 [deploy/rz-mcp.service](deploy/rz-mcp.service)。

## 工具

| 工具 | 用途 |
|---|---|
| `list_documents` | 列出某 device 适用的文档，以及哪些模块有结构化寄存器数据 |
| `search_docs` | 在所有文档中全文检索，返回 doc_id、Rev、章节号、页码和片段 |
| `get_toc` / `get_section` | 浏览章节树；读取某个章节的全文（带 `[p.N]` 页码标记） |
| `read_pages` | 读取指定页的原文（每次最多 5 页） |
| `lookup_register` | 按名称或绝对地址精确查找寄存器，返回偏移、各地址空间的地址、复位值、全部位域、出处和校验结果 |
| `search_registers` | 按功能描述或字段名查找寄存器 |

结构化寄存器目前覆盖 WDT、SCIFA、CPG，其他模块请用 `search_docs` 和 `get_section`。

**BSP 版本与源码**（B1c）。这几个工具读取的是 DL_DIR 里 git 镜像中的官方 rev，不会读 work-shared 下带着 PoC 补丁的源码树：

| 工具 | 用途 |
|---|---|
| `get_bsp_release` | 官方 BSP 的信息：各层 pin、TEMPLATECONF、kernel / U-Boot / TF-A 的 rev（会和本地 meta-renesas 对照）、专有包，以及 eMMC 和 QSPI 的写入地址 |
| `bsp_source` / `bsp_grep` | 读取官方 rev 上的文件或目录，或在其中检索 |
| `patch_stack` | 各层在官方 rev 之上加了哪些补丁；对于内核，还会把上次构建实际打上的每个提交，对应到出自哪个层的哪个补丁 |
| `check_build_env` | 对比当前构建配置和官方发布：模板、MACHINE、DISTRO、额外的层及其所属的 PoC、RM_WORK_EXCLUDE、各组件的 pin 和镜像 |

## Prompt：PoC 工作流

| Prompt | 用途 |
|---|---|
| `/rz-docs:poc <名称>` | 推进 RZ/G3L PoC 工作流的第 1～4 步：设想 → v1、v2 → 两轨方案 → 融合 → 用户对齐。它读取 PoC 仓库在磁盘上的状态，告诉主会话下一步做什么 |

工具、模板和子 Agent 的定义都在 [workflow/](workflow/README.md) 下。流程的定义见 `plan/PoC_Workflow_Plan.md`。
