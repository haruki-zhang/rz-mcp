# workflow/build/：构建与交付（第 5 步，B2）

所有 PoC 共用同一个 Yocto 项目和构建目录（默认是 `rzg3l-bsp-v1/build`）。下面三个脚本负责：切换到某个 PoC 的配置、在 Ubuntu 22.04 容器里构建、按 rzg3l-lowpower-display 的样式打包交付物。

| 脚本 | 作用 |
|---|---|
| `poc-use <poc>` | 生成 `build/conf`：官方模板（`bsp-plus-conf`）+ rz-mcp 标准项 + `<poc>/build-conf`。旧文件备份到 `build/conf/backup/<时间>/`。bitbake 正在运行时拒绝执行 |
| `poc-use --capture <poc>` | 反过来：从当前的 `build/conf` 提取出 `<poc>/build-conf`，适用于原来手工配置的 PoC |
| `poc-use --baseline` | 只用官方模板和标准项 |
| `poc-build <target> [--task populate_sdk]` | 在 `max-ubuntu22.04` 容器里以 `nice` 运行 bitbake（主机是 26.04，BSP 只在 22.04 上测过）。会写 `data/build-runs/<时间>-<target>/`，里面有 `bitbake.log` 和 `report.json`：使用的配置、任务统计、错误、新生成的文件 |
| `poc-package <poc> --image <x>.wic.gz` | 生成 `<poc>/dist/<repo>-<版本>/`，内容是 README、docs（md 和 pdf）、layers、image、tools，另加 `versions.json` 和 `SHA256SUMS`。不包含 `design/`、`build-conf/`、`.git`。仓库还是空骨架时拒绝打包 |

## build-conf 的格式

```text
<poc>/build-conf/layers       每行一个层，路径相对于 Yocto 项目目录；'#' 后面是注释
<poc>/build-conf/local.conf   原样追加到 local.conf 的末尾
```

标准项（`poc-use` 总会加上）有两条：

- `MACHINE = "smarc-rzg3l"`
- `RM_WORK_EXCLUDE += "linux-renesas u-boot trusted-firmware-a"`

第二条用来保留 kernel、U-Boot、TF-A 的 work 目录，这样 MCP 的 `patch_stack` 和设计 Agent 才能看到实际打上的补丁。

## 典型流程

```sh
workflow/build/poc-use rzg3l-<名称>
workflow/build/poc-build <image>
workflow/board/evk-flash emmc --image <deploy>/<image>-smarc-rzg3l.rootfs-<时间>.wic.gz    # 第 6 步
workflow/build/poc-package rzg3l-<名称> --image <同一个 wic.gz> --version v1.0
```

所有 PoC 共用一个 TMPDIR，deploy 目录里的产物会被下一次构建覆盖。所以要在构建完成后立刻打包，并且打包时用的必须是同一个镜像。`versions.json` 记录的是打包那一刻的 `build/conf` 和各层版本。

## 测试

```sh
/usr/bin/python3 eval/test_b1c_b2.py
```

这是已知答案测试，只读取真实的 Yocto 项目，写入的东西都在 `/tmp` 下：
- `poc-use` 的往返测试：先提取当前配置，再在临时构建目录里重新生成，结果必须和真实的 `build/conf` 等价；
- `poc-package` 的测试：打包 rzg3l-lowpower-display，目录结构必须和它本身一致；
- 另外还有 B1c 各个 MCP 工具的测试。

## 尚未实现

- **洁净室复现**：在新容器、新构建目录里，只用交付包和官方 git 重新构建。这一步要几个小时，还要临时占用约 60 GB 磁盘，计划只在正式交付前做一次。
