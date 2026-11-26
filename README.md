# rz-mcp — RZ/G3L 开发 MCP 服务器 rz-dev（文档查阅、Yocto、PoC、上板）

改造计划见 [plan/MCP_Rebuild_Plan.md](plan/MCP_Rebuild_Plan.md)。目前完成到 P1：文档工具可用；Yocto、PoC、上板工具的接口已定稿，调用时返回"尚未实现"；作业框架（`job_status`、`job_cancel`）可用。

## 目录结构

```
catalog/manifest.toml   文档目录（唯一的元数据来源）：doc_id、Rev、类型、适用的 device/board、索引方式
docs/                   原始资料（只读），按归属分目录
  devices/<device>/       器件专属：HW UM、Datasheet、App Note
  boards/<board>/         板级：EBM、原理图、BOM、勘误、设计数据
  family/<family>/        跨器件：DRAM list、Linux 驱动手册
src/rzmcp/              索引器、查询层、MCP server（server.py 文档工具，devtools.py 开发工具，jobs.py 作业框架，config.py 机器配置）
config.toml             本机配置（不进 git，模板 config.example.toml）：Yocto 项目、构建容器、串口、USB 网卡、TFTP、PoC 根目录
data/rz.db              生成物（可随时重建），另有 calls.jsonl（调用日志）、.token、jobs/（作业目录）、recognition/（识别测试结果）
eval/                   冒烟评测、作业框架测试（test_jobs.py）、识别测试（recognition.py）
plan/                   计划书、可行性报告、W3 spike 报告；archive/ 下是已移除功能（PoC 工作流、BSP 源码工具）的旧设计记录
```

`docs/devices/rzg3l/` 里还有硬件手册的机密版 R01UH1152EJ0110（Additional document，每页标有 CONFIDENTIAL）。它只在 manifest 里登记，没有建索引；默认的硬件手册仍然是公开版 R01UH1153EJ0110。

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
.venv/bin/python eval/test_jobs.py             # 作业框架测试（不需要硬件，约 30 秒）
.venv/bin/python eval/recognition.py           # 识别测试：无关目录下的新会话能否选对工具（16 条 × 5 次，约 2 分钟，消耗 token）
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
claude mcp add --scope user --transport http rz-dev http://localhost:8765/mcp \
  --header "Authorization: Bearer $(cat data/.token)"
claude mcp list          # 应显示 rz-dev ... ✔ Connected
```

换 token 之后，先执行 `claude mcp remove rz-dev -s user`，再重新 add。从旧名字迁移：`claude mcp remove rz-docs -s user` 后按上面的命令 add。

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

## 工具（19 个）

| 组 | 工具 | 状态 |
|---|---|---|
| 文档 | `list_documents`、`search_docs`、`get_toc`、`get_section`、`read_pages`、`lookup_register`、`search_registers` | 可用 |
| Yocto | `yocto_status`、`yocto_setup` | 接口已定稿（P3） |
| PoC | `poc_create`、`poc_status`、`poc_build`、`poc_package` | 接口已定稿（P3、P4） |
| 上板 | `board_status`、`board_flash`、`board_exec`、`board_verify` | 接口已定稿（P2） |
| 作业 | `job_status`、`job_cancel` | 可用 |

结构化寄存器目前覆盖 WDT、SCIFA、CPG，其他模块请用 `search_docs` 和 `get_section`。

### 作业

搭环境、构建、打包、写板、验证都是作业：工具立即返回 `job_id`，之后用 `job_status(job_id, wait_seconds=50)` 查询，直到结束。
作业的全部状态在 `data/jobs/<id>/`（`job.json`、`status.json`、`log.txt`、`report.md`）。
作业进程独立于服务器：服务器作为 systemd 服务运行时，每个作业用 `systemd-run --user` 启动为单独的 unit（`rz-job-<id>`），所以重启服务不会中断作业，服务的资源上限也不作用于作业；在终端里运行服务器时，作业脱离为独立进程。
串口这类独占资源由作业进程用 `flock` 持有，第二个请求会直接得到"正被作业 X 占用"。
