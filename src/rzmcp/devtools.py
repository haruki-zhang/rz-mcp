"""The development tools of rz-dev: Yocto, PoC, board and job tools (interfaces; implementations land per
work package). Every tool works on its own: inputs are a PoC name or absolute paths, defaults come from
config.toml, and no tool checks which tool ran before it.
"""
# no `from __future__ import annotations`: MCPServer builds tool schemas from the real annotation objects
from typing import Annotated, Literal, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from . import jobs, yocto
from . import poc as pocs   # the module; `poc` is a tool parameter name

COMPONENTS = Literal["graphics", "codecs", "security", "multi-os"]
PocArg = Annotated[str, Field(description="PoC name (the directory name under the configured PoC root), e.g. 'wdt-reboot'")]
OptPocArg = Annotated[Optional[str], Field(description="Optional PoC name; when given, the result is also recorded in that PoC's state")]


def _todo(tool: str, package: str) -> str:
    return (f"{tool} 尚未实现（rz-mcp 改造计划工作包 {package}）。接口已经定稿，功能还没有接上。"
            f"请告诉用户这一步目前需要手动完成。")


def register(mcp, logged) -> None:
    ro = {"annotations": ToolAnnotations(read_only_hint=True, open_world_hint=False), "structured_output": False}
    act = {"annotations": ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False),
           "structured_output": False}
    hw = {"annotations": ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
          "structured_output": False}

    # ------------------------------------------------------------ Yocto
    @mcp.tool(**ro)
    @logged
    def yocto_status() -> str:
        """Check the RZ/G3L Yocto / BSP build environment (Yocto 环境、构建环境、BSP 源码在哪): whether the shared
        Yocto project exists and is usable, which optional components (graphics, codecs, security, multi-os) are
        installed, whether a bitbake build is running, and where the BSP sources (Linux kernel, U-Boot, TF-A,
        meta-renesas, ...) and the build outputs are on disk. Read those sources directly with your own file
        tools. If the project is missing or broken, call yocto_setup."""
        return yocto.tool_status()

    @mcp.tool(**act)
    @logged
    def yocto_setup(components: Annotated[Optional[list[COMPONENTS]], Field(description="Optional components to install (licensed packages from the configured packages directory). Omit to keep what is installed.")] = None) -> str:
        """Set up, create or repair the RZ/G3L Yocto build environment (搭建 Yocto 环境、安装 BSP、加可选组件)
        shared by all PoCs, and install optional components (graphics, codecs, security, multi-os). Reuses an
        existing project and is safe to call again after an interruption. Starts a background job: returns a
        job_id at once; follow it with job_status."""
        return yocto.tool_setup(components)

    # ------------------------------------------------------------ PoC
    @mcp.tool(**act)
    @logged
    def poc_create(name: Annotated[str, Field(description="Short PoC name: lowercase letters, digits and '-', e.g. 'wdt-reboot'")],
                   summary: Annotated[str, Field(description="One sentence: what the PoC should demonstrate")]) -> str:
        """Start a new PoC / proof of concept / demo on RZ/G3L (新建 PoC、做一个演示、概念验证): creates the PoC
        repository with a design template (design.md, including the on-board verification plan), a documentation
        template and an empty Yocto layer. Then research with the document tools and the BSP sources (locations
        from yocto_status), write the design and verification plan, and get the user's confirmation before
        implementing (only then set the design's first line to 状态：已确认 <date>)."""
        try:
            return pocs.create(name, summary)
        except pocs.PocError as e:
            return str(e)

    @mcp.tool(**ro)
    @logged
    def poc_status(name: Annotated[Optional[str], Field(description="PoC name; omit to list all PoCs")] = None) -> str:
        """Where is a PoC and what to do next (PoC 进度、进行到哪一步、下一步做什么): without a name lists all PoCs;
        with a name shows the current step, the results of the last build, flash and verification, and exactly
        which tool to call next. Use it to pick up a PoC in a new session."""
        try:
            return pocs.status_text(name)
        except pocs.PocError as e:
            return str(e)

    @mcp.tool(**act)
    @logged
    def poc_build(poc: Annotated[Optional[str], Field(description="PoC name: apply its layer and configuration first. Omit to build the official BSP image.")] = None,
                  image: Annotated[Optional[str], Field(description="Image recipe, e.g. 'core-image-minimal'. Default: the PoC's image, else the configured default image.")] = None) -> str:
        """Build an RZ/G3L Linux image with Yocto / bitbake (构建镜像、编译、bitbake). With a PoC name it applies
        that PoC's layer and configuration first; without one it builds the official BSP image. Starts a
        background job: returns a job_id at once; follow it with job_status. On success the result lists the
        image files (give them to board_flash); on failure an error summary."""
        return yocto.tool_build(poc, image)

    @mcp.tool(**act)
    @logged
    def poc_package(poc: PocArg,
                    version: Annotated[str, Field(description="Delivery version label")] = "v1.0") -> str:
        """Package a PoC for delivery (交付包、打包、交付物): layers/, docs/ (Markdown and PDF), image/, README,
        version manifest and checksums. Licensed packages are never included. Starts a background job: returns
        a job_id at once; follow it with job_status. Refuses (writing nothing) while the docs are still the
        template, the layer is empty or there is no successful build matching the layers."""
        try:
            return pocs.package_start(poc, version)
        except pocs.PocError as e:
            return str(e)

    # ------------------------------------------------------------ board
    @mcp.tool(**ro)
    @logged
    def board_status() -> str:
        """Is the RZ/G3L EVK board connected and what state is it in (板子状态、板子能不能启动、连上了吗): checks the
        host side (serial console, USB network adapter, TFTP) and detects the board state (no response, SCIF
        download mode, Flash Writer, U-Boot, Linux). Writes nothing. Missing host configuration is reported with
        the exact command to fix it. Not needed before board_flash / board_verify / board_exec, which check the
        board themselves."""
        return _todo("board_status", "P2")

    @mcp.tool(**hw)
    @logged
    def board_flash(parts: Annotated[Literal["rootfs", "bootloader", "all"], Field(description="What to write: 'rootfs' (the .wic.gz image, always to eMMC), 'bootloader' (BL2 + FIP to boot_device), or 'all'")],
                    boot_device: Annotated[Literal["emmc", "qspi"], Field(description="Where the bootloader goes and which boot mode the board ends up in")] = "emmc",
                    image: Annotated[Optional[str], Field(description="Absolute path of the .wic.gz rootfs image. Default: the newest image built in the shared Yocto project (or the PoC's last build).")] = None,
                    bl2: Annotated[Optional[str], Field(description="Absolute path of BL2 (.srec). Default: found next to the image.")] = None,
                    fip: Annotated[Optional[str], Field(description="Absolute path of FIP (.srec). Default: found next to the image.")] = None,
                    flash_writer: Annotated[Optional[str], Field(description="Absolute path of the Flash Writer (.mot), only needed for SCIF download. Default: found next to the image.")] = None,
                    boot_to: Annotated[Literal["login", "uboot"], Field(description="Boot this far afterwards to confirm the result")] = "login",
                    poc: OptPocArg = None) -> str:
        """Flash / burn / write an image to the RZ/G3L EVK board (烧录、烧写、写板、刷机、烧到板子): eMMC or QSPI,
        bootloader (BL2 + FIP) and/or rootfs. Call it directly: it runs all host, network, file and board-state
        checks itself (no board_status or job_status needed first), and without `image` it takes the newest image
        built in the shared Yocto project ("the image I just built"). Works with any image file, no PoC needed.
        Continues from whatever state the board is in, verifies every write and boots to login (or U-Boot) to
        confirm. Starts a background job: returns a job_id and the complete list of
        physical operations (DIP switches, power) the user will have to do; follow it with job_status."""
        return _todo("board_flash", "P2")

    @mcp.tool(**hw)
    @logged
    def board_exec(command: Annotated[str, Field(description="One command, e.g. 'mmc info' (U-Boot) or 'dmesg | grep wdt' (Linux)")],
                   where: Annotated[Literal["uboot", "linux"], Field(description="Run it at the U-Boot prompt or in the board's Linux shell")],
                   timeout_s: Annotated[int, Field(description="Seconds to wait for the command to finish (1-600)")] = 30) -> str:
        """Run a command on the EVK board (在板子上执行命令、在 U-Boot 里执行) in U-Boot or in Linux over the serial
        console and return its output. For experiments and debugging. If the board is not in the requested state
        it says which state it is in and how to get there."""
        return _todo("board_exec", "P2")

    @mcp.tool(**hw)
    @logged
    def board_verify(plan: Annotated[str, Field(description="Absolute path of a verification plan (verify.toml), or a PoC name to use that PoC's plan")]) -> str:
        """Verify / test on the EVK board (上板验证、在板子上跑验证、测试): runs a verification plan whose items run on
        the board's Linux, on the host, or measure boot times, each judged only by explicit criteria (expected
        output, return code, time limit), and writes a verification report with the raw logs. Starts a
        background job: returns a job_id at once; follow it with job_status."""
        return _todo("board_verify", "P2")

    # ------------------------------------------------------------ jobs
    @mcp.tool(**ro)
    @logged
    def job_status(job_id: Annotated[Optional[str], Field(description="Job id; omit to list running and recent jobs")] = None,
                   wait_seconds: Annotated[int, Field(description="Wait up to this many seconds (max 50) for the job to change state before answering")] = 0) -> str:
        """Progress and result of a background job (作业进度、刚才的构建/写板/验证怎么样了) started by yocto_setup,
        poc_build, poc_package, board_flash or board_verify. While a job runs, call it again with wait_seconds=50
        until it ends. When it reports waiting_human, tell the user the instruction word for word and keep
        polling; the user does not need to reply. Without job_id it lists running and recent jobs."""
        if not job_id:
            items = jobs.list_jobs()
            if not items:
                return "没有作业记录。"
            return "\n".join(f"- {s['id']}  {s.get('title', '')}  {s.get('state')}"
                             + (f"  第 {s.get('step', 0)}/{s['of']} 步" if s.get("of") else "")
                             for s in items) + "\n用 job_status(job_id=...) 查看某个作业的详情。"
        try:
            return jobs.format_status(jobs.wait(job_id, wait_seconds))
        except KeyError:
            return f"没有作业 {job_id}。调用 job_status() 不带参数可以列出现有作业。"

    @mcp.tool(**act)
    @logged
    def job_cancel(job_id: Annotated[str, Field(description="Job id from job_status")]) -> str:
        """Cancel / stop a running background job (取消、停止构建或写板). Calling the tool that started it again
        later continues from the current state."""
        try:
            return jobs.format_status(jobs.cancel(job_id))
        except KeyError:
            return f"没有作业 {job_id}。调用 job_status() 不带参数可以列出现有作业。"
