# workflow/build/：构建与交付（第 5 步，B2）

默认所有 PoC 共用同一个 Yocto 项目和构建目录（`rzg3l-bsp-v1/build`）。也可以给某个 PoC 指定另一个项目目录：目录不存在或为空时，由 `yocto-project` 自动搭建；已经是可用的项目时，直接复用。各个项目的下载和构建缓存相互独立，不共享。

下面的脚本负责：准备 Yocto 项目、切换到某个 PoC 的配置、在 Ubuntu 22.04 容器里构建、按 rzg3l-lowpower-display 的样式打包交付物。除 `yocto-project` 外，都用 `--project DIR` 指定项目，默认是 `$RZ_POC_PROJECT`，没有设置时是 `rzg3l-bsp-v1`。

| 脚本 | 作用 |
|---|---|
| `yocto-project check [DIR]` | 判断目录的状态，不做任何改动：不存在或为空（`.claude/`、`.vscode/` 和 PoC 仓库不算）、上次搭建没有完成、可用、不可用 |
| `yocto-project setup DIR` | 让目录成为可用的项目。已经可用的直接复用，只补上缺的部分；不存在或为空的，按 `catalog/bsp_releases.toml` 拉取 poky、meta-arm、meta-openembedded、meta-renesas 并核对 pin，用 `poc-use --baseline` 写入 build/conf，再在容器里取回内核、U-Boot、TF-A 的官方源码（供 `bsp_source` 等使用）；中断后再运行一次会接着做；不可用的目录一律拒绝，不做任何改动。实测（2026-11-26）约 6 分钟、4.3 GB，几乎都是下载，网络慢时会更久。不构建镜像 |
| `yocto-project add DIR <组件>…` | 往可用的项目里装可选组件：`graphics`、`codecs`、`security`、`multi-os` |
| `poc-use <poc>` | 生成 `build/conf`：官方模板（`bsp-plus-conf`）+ rz-mcp 标准项 + `<poc>/build-conf`。`build-conf/layers` 里有项目还没有的可选组件层时，先自动安装它（`--dry-run` 只报告）。旧文件备份到 `build/conf/backup/<时间>/`。bitbake 正在运行时拒绝执行 |
| `poc-use --capture <poc>` | 反过来：从当前的 `build/conf` 提取出 `<poc>/build-conf`，适用于原来手工配置的 PoC |
| `poc-use --baseline` | 只用官方模板和标准项 |
| `poc-build <target> [--task populate_sdk]` | 在 `max-ubuntu22.04` 容器里以 `nice` 运行 bitbake（主机是 26.04，BSP 只在 22.04 上测过）。会写 `data/build-runs/<时间>-<target>/`，里面有 `bitbake.log` 和 `report.json`：使用的配置、任务统计、错误、新生成的文件 |
| `poc-env [--project DIR] <命令…>` | 在容器里加载 BitBake 环境后执行一条命令，例如 `devtool modify linux-renesas`、`devtool finish linux-renesas ../rzg3l-<名称>/layers/meta-rzg3l-<名称>`、`bitbake -e`。参数以列表的形式原样传入，不经过 shell。bitbake 正在运行时，拒绝执行 bitbake 和 devtool 命令 |
| `src-snapshot [--project DIR] [--artifacts]` | 给项目拍源码快照，供 `bsp_source`、`bsp_grep`、`patch_stack`、`build_facts`、`bsp_recipe` 使用：用官方配置（发布的层加上已安装的可选组件，不含 PoC 层）运行 `bitbake -e`，生成补丁后的源码视图；加 `--artifacts` 时，在临时目录里编译内核、配置 U-Boot，保留 `.config`、编译对象清单和设备树。不写入项目。约 2 分钟，加 `--artifacts` 约 10 分钟。搭建项目或安装可选组件后要重新运行 |
| `poc-package <poc> --image <x>.wic.gz` | 生成 `<poc>/dist/<repo>-<版本>/`，内容是 README、docs（md 和 pdf）、layers、image、tools，另加 `versions.json` 和 `SHA256SUMS`。不包含 `design/`、`build-conf/`、`.git`。仓库还是空骨架时拒绝打包 |

## 可选组件

官方模板里没有、按需安装的组件，登记在 `catalog/bsp_releases.toml` 的 `optional` 里：

| 组件 | 来源 | 层 |
|---|---|---|
| `graphics` | 授权软件包 RTK0EF0045Z14001ZJ 5.1.3.2 | `meta-rz-features/meta-rz-graphics` |
| `codecs` | 授权软件包 RTK0EF0045Z16002ZJ 5.1.5.0（含内层的 prebuilt 包） | `meta-rz-features/meta-rz-codecs` |
| `security` | 授权软件包 RTK0EF0121Z00000ZJ 6.12-v1.0（需要 Renesas 批准才能获取） | `meta-rz-features/meta-rz-security` |
| `multi-os` | git：github.com/renesas-rz/meta-rz-multi-os，tag v4.0.0 | `meta-rz-features/meta-rz-multi-os/meta-rzg3l` |

- 授权软件包只读地放在 `rz-mcp/packages/<BSP 版本>/` 下，不进 git，也不能放进 PoC 的交付包。安装前先核对 catalog 里的 sha256，不一致就拒绝；解压时只接受 `meta-rz-features/` 下的内容。先解压到 `<项目>/.rz-mcp/stage/`，完整之后才移到正式位置。
- 装了什么，记录在 `<项目>/.rz-mcp/project.json`。
- security 包只安装层；`TRUSTED_BOARD_BOOT`、`ENABLE_SPD_OPTEE`、`ENABLE_RZ_SCE` 和密钥，由 PoC 的 `build-conf/local.conf` 和层自己提供（见包里的 Security Startup Guide）。
- rzg3l-bsp-v1 的 multi-os 是旧的目录结构（`meta-rz-multi-os/meta-rzg/meta-rzg3l`）。PoC 的 `build-conf/layers` 写的是这个旧路径时，在新项目里 `poc-use` 会换成新路径。

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
workflow/build/yocto-project setup /home/mpu-epmaj/max/workspace/<新项目>   # 只在用新项目时；已可用则直接复用
workflow/build/poc-use rzg3l-<名称> --project <项目>
workflow/build/poc-build --project <项目> <image>
workflow/board/evk-flash emmc --image <deploy>/<image>-smarc-rzg3l.rootfs-<时间>.wic.gz    # 第 6 步
workflow/build/poc-package rzg3l-<名称> --image <同一个 wic.gz> --version v1.0
```

所有 PoC 共用一个 TMPDIR，deploy 目录里的产物会被下一次构建覆盖。所以要在构建完成后立刻打包，并且打包时用的必须是同一个镜像。`versions.json` 记录的是打包那一刻的 `build/conf` 和各层版本。

## 测试

```sh
/usr/bin/python3 eval/test_b1c_b2.py
python3 eval/test_yocto_project.py      # 项目的判定、搭建和复用、可选组件、poc-use 自动安装、第 0 步
```

`test_yocto_project.py` 用指向 rzg3l-bsp-v1 的符号链接拼出假项目，只有 multi-os 会真的 git clone。已知答案：从包里装出来的 graphics 和 codecs、v4.0.0 的 multi-os，都必须和 rzg3l-bsp-v1 里的完全一致。

这是已知答案测试，只读取真实的 Yocto 项目，写入的东西都在 `/tmp` 下：
- `poc-use` 的往返测试：先提取当前配置，再在临时构建目录里重新生成，结果必须和真实的 `build/conf` 等价；
- `poc-package` 的测试：打包 rzg3l-lowpower-display，目录结构必须和它本身一致；
- 另外还有 B1c 各个 MCP 工具的测试。

## 尚未实现

- **洁净室复现**：在新容器、新构建目录里，只用交付包和官方 git 重新构建。这一步要几个小时，还要临时占用约 60 GB 磁盘，计划只在正式交付前做一次。
