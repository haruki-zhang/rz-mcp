"""MCP server exposing the RZ documentation database.

    # localhost (Streamable HTTP, bearer-token protected) — default; URL http://localhost:8765/mcp
    python -m rzmcp.server --transport http --port 8765
    # LAN (only if the MCP allowlist permits non-localhost URLs)
    python -m rzmcp.server --transport http --host 0.0.0.0 --port 8765
    # local (stdio) — for Claude Code on this PC
    python -m rzmcp.server --transport stdio

Token: $RZ_MCP_TOKEN, else data/.token (created on first start, mode 600).
Every tool call is appended to data/calls.jsonl (tool, args, size, duration) for later review.
"""
# no `from __future__ import annotations`: MCPServer builds tool schemas from the real annotation objects
import argparse
import functools
import hmac
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Annotated, Optional

from mcp.server.mcpserver import MCPServer          # mcp >= 2 (FastMCP was renamed to MCPServer)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from . import DB_PATH, ROOT
from . import query as Q
from . import workflow as W
from . import bsp as B

LOG_PATH = ROOT / "data" / "calls.jsonl"

INSTRUCTIONS = """\
Renesas RZ documentation server (pilot: RZ/G3L). Indexed: RZ/G3L hardware manual (HW UM, public edition), \
datasheet, app notes, EVK quick start guide, SMARC module / Carrier Board II manuals, schematics and BOMs, \
and RZ Linux device-driver manuals.

How to use:
- Register questions for structured modules (WDT, SCIFA, CPG): lookup_register / search_registers give exact \
offsets, absolute addresses, reset values and bit fields.
- Everything else (other modules, procedures, electrical values, boards, Linux drivers): search_docs, then \
get_section or read_pages to read the actual text before answering.
- Documents are English; search with English keywords and register/module abbreviations.
- Always cite doc_id, Rev and page numbers from the tool output. Page numbers are PDF page indices.
- If a tool reports that something is not found, say so. Do not fill in register values from memory.
"""

DeviceArg = Annotated[str, Field(description="Device id, e.g. 'rzg3l' (default). Filters documents that apply to this device.")]


def _logged(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        err = None
        try:
            out = fn(*args, **kwargs)
            return out
        except Exception as e:          # surface as tool error text, keep the server alive
            err = f"{type(e).__name__}: {e}"
            out = f"Internal error in {fn.__name__}: {err}"
            return out
        finally:
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": fn.__name__, "args": kwargs,
                   "chars": len(out) if isinstance(out, str) else None, "ms": int((time.time() - t0) * 1000)}
            if err:
                rec["error"] = err
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass
    return wrapper


def build_server() -> MCPServer:
    mcp = MCPServer(name="rz-docs", instructions=INSTRUCTIONS, version="0.1.0")
    # plain-text results only (no structuredContent wrapper); all tools are read-only lookups
    ro = {"annotations": ToolAnnotations(read_only_hint=True, open_world_hint=False), "structured_output": False}

    @mcp.tool(**ro)
    @_logged
    def list_documents(device: DeviceArg = "rzg3l",
                       doc_type: Annotated[Optional[str], Field(description="Optional filter: hw_manual, datasheet, app_note, quick_start, board_manual, schematic, bom, errata, sw_manual, component_datasheet")] = None) -> str:
        """List the documents indexed for a device (doc_id, type, revision, pages, title) and which modules have
        structured register data. Use this to learn what sources exist or to get a doc_id."""
        return Q.list_documents(device=device, doc_type=doc_type)

    @mcp.tool(**ro)
    @_logged
    def search_docs(query: Annotated[str, Field(description="English keywords, register/module/pin/signal names. E.g. 'boot mode pin', 'SCIF module stop', 'VDDQ_DDR operating range'")],
                    device: DeviceArg = "rzg3l",
                    doc_type: Annotated[Optional[str], Field(description="Optional doc type filter (see list_documents)")] = None,
                    doc_id: Annotated[Optional[str], Field(description="Optional: restrict to one document id")] = None,
                    limit: Annotated[int, Field(description="Max results (1-20)")] = 8) -> str:
        """Full-text search over all indexed documents (manual sections, schematic sheets, BOM rows).
        Returns ranked hits with doc_id, revision, section number, page and a snippet. Read the hit with
        get_section or read_pages before answering; snippets alone are not enough to quote values."""
        return Q.search_docs(query=query, device=device, doc_type=doc_type, doc_id=doc_id, limit=limit)

    @mcp.tool(**ro)
    @_logged
    def get_toc(doc_id: Annotated[Optional[str], Field(description="Document id; default = the device's hardware manual")] = None,
                under: Annotated[Optional[str], Field(description="Section number to expand, e.g. '5.4' or '4.4.3'. Omit for top level.")] = None,
                depth: Annotated[int, Field(description="Levels below 'under' to show (1-4)")] = 1,
                device: DeviceArg = "rzg3l") -> str:
        """Show the section tree (table of contents) of a document with page ranges, optionally below a given
        section. Use it to navigate a chapter or to check whether a section exists."""
        return Q.get_toc(doc_id=doc_id, under=under, depth=depth, device=device)

    @mcp.tool(**ro)
    @_logged
    def get_section(section: Annotated[str, Field(description="Section number like '7.4.2.5' (preferred) or a title fragment")],
                    doc_id: Annotated[Optional[str], Field(description="Document id; default = the device's hardware manual")] = None,
                    device: DeviceArg = "rzg3l",
                    offset: Annotated[int, Field(description="Character offset for paging through long sections")] = 0,
                    max_chars: Annotated[int, Field(description="Max characters to return (1000-10000)")] = 8000) -> str:
        """Return the text of one section (including its subsections) with page markers [p.N], its page range,
        breadcrumb and list of subsections. Long sections are paged: call again with the offset given."""
        return Q.get_section(section=section, doc_id=doc_id, device=device, offset=offset, max_chars=max_chars)

    @mcp.tool(**ro)
    @_logged
    def read_pages(doc_id: Annotated[str, Field(description="Document id from search results or list_documents")],
                   start_page: Annotated[int, Field(description="First PDF page (1-based)")],
                   end_page: Annotated[Optional[int], Field(description="Last page (inclusive, max 5 pages per call)")] = None) -> str:
        """Return the raw text of specific PDF pages (layout-preserving, running headers removed). Use it to read
        tables or to verify a value at the cited page."""
        return Q.read_pages(doc_id=doc_id, start_page=start_page, end_page=end_page)

    @mcp.tool(**ro)
    @_logged
    def lookup_register(query: Annotated[str, Field(description="Register name (e.g. 'WDTSET_2', 'WDTSET', 'WDTSET_n', 'CPG_SDHI_DDIV', 'SMR') or an absolute address (e.g. '0x100AC008', \"H'0_1280_0804\")")],
                        device: DeviceArg = "rzg3l",
                        module: Annotated[Optional[str], Field(description="Optional module filter: WDT, SCIFA, CPG")] = None,
                        address_space: Annotated[Optional[str], Field(description="Optional: CA55, CM33_S (secure), CM33_NS (non-secure); comma-separated allowed")] = None) -> str:
        """Exact register lookup in the structured register database (currently modules WDT, SCIFA, CPG of the
        RZ/G3L HW UM). Returns offset, absolute address per channel and address space, size, access, reset value,
        every bit field (bits, name, access, reset, description), the source section/pages and the result of the
        automatic extraction checks. For registers of other modules it says 'not found' — then use search_docs."""
        return Q.lookup_register(query=query, device=device, module=module, address_space=address_space)

    @mcp.tool(**ro)
    @_logged
    def search_registers(query: Annotated[str, Field(description="Words describing the function or a field name, e.g. 'stop bit length', 'FIFO trigger', 'SDHI divider', 'TEND'")],
                         device: DeviceArg = "rzg3l",
                         module: Annotated[Optional[str], Field(description="Optional module filter: WDT, SCIFA, CPG")] = None,
                         limit: Annotated[int, Field(description="Max results (1-30)")] = 12) -> str:
        """Find registers / bit fields by function or partial name within the structured modules (WDT, SCIFA,
        CPG). Returns candidate registers with offset and section; follow up with lookup_register."""
        return Q.search_registers(query=query, device=device, module=module, limit=limit)

    CompArg = Annotated[str, Field(description="Component: 'kernel' (rz_linux-cip), 'u-boot' (renesas-u-boot-cip) or 'tf-a' (rzg_trusted-firmware-a)")]

    @mcp.tool(**ro)
    @_logged
    def get_bsp_release(release: Annotated[str, Field(description="Release id; default the RZ/G3L BSP Plus release")] = "RZG3L-BSP-1.0.0") -> str:
        """The RZ/G3L BSP release as Renesas publishes it: git layer pins, TEMPLATECONF, the kernel / U-Boot / TF-A
        revisions meta-renesas pins for rzg3l-family (checked against the Yocto project), proprietary packages,
        and the boot media layout (eMMC boot partition sectors, EXT_CSD, QSPI addresses, SW_MODE)."""
        return B.get_bsp_release(release)

    @mcp.tool(**ro)
    @_logged
    def bsp_source(component: CompArg,
                   path: Annotated[str, Field(description="Path in the repository, e.g. 'drivers/watchdog/rzg2l_wdt.c', 'arch/arm64/boot/dts/renesas', 'configs/smarc-rzg3l_defconfig'. A directory lists its entries.")],
                   start_line: Annotated[int, Field(description="First line to return")] = 1,
                   max_lines: Annotated[int, Field(description="Lines to return (max 2000)")] = 300) -> str:
        """Read a file (or list a directory) of the OFFICIAL kernel / U-Boot / TF-A source at the revision the BSP
        pins, from bitbake's git mirror. Use this instead of build/tmp/work-shared, which carries PoC patches."""
        return B.bsp_source(component, path, start_line, max_lines)

    @mcp.tool(**ro)
    @_logged
    def bsp_grep(component: CompArg,
                 pattern: Annotated[str, Field(description="Extended regular expression, e.g. 'renesas,r9a08g046' or 'rzg2l_wdt_probe'")],
                 path: Annotated[Optional[str], Field(description="Optional path prefix to search in, e.g. 'drivers/net'")] = None,
                 limit: Annotated[int, Field(description="Max hits (1-300)")] = 60) -> str:
        """git grep in the OFFICIAL kernel / U-Boot / TF-A source at the BSP's revision. Returns path:line: text."""
        return B.bsp_grep(component, pattern, path, limit)

    @mcp.tool(**ro)
    @_logged
    def patch_stack(component: CompArg) -> str:
        """What the active layers (build/conf/bblayers.conf) add on top of the official revision: patch / config
        fragment files per layer with their subject, and for the kernel the commits the last build applied, each
        traced to the layer whose patch it came from."""
        return B.patch_stack(component)

    @mcp.tool(**ro)
    @_logged
    def check_build_env() -> str:
        """Compare the Yocto project's build configuration with the BSP release: TEMPLATECONF, MACHINE, DISTRO,
        official and extra layers (and which PoC repository owns each), RM_WORK_EXCLUDE, layer checkouts,
        component revisions and git mirrors, whether bitbake is running."""
        return B.check_build_env()

    @mcp.prompt(name="poc", title="RZ/G3L PoC workflow (steps 1-6)",
                description="推进 RZ/G3L PoC 工作流的第 1～6 步：设想 → v1/v2 → 两轨方案 → 融合 → 对齐 → 实现、构建 → 写板子、验证 → 打包。"
                            "参数：PoC 名称（一个词，例如 lowpower-display）。每完成一步再运行一次即可继续。")
    @_logged
    def poc(name: str) -> str:
        """Steps 1-6 of the PoC workflow; reads the PoC repository state and returns the next step."""
        return W.poc_prompt(name)

    return mcp


class BearerAuth:
    """Minimal ASGI middleware: every HTTP request must carry 'Authorization: Bearer <token>'."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def load_token() -> str:
    tok = os.environ.get("RZ_MCP_TOKEN")
    if tok:
        return tok
    p = ROOT / "data" / ".token"
    if p.exists():
        return p.read_text().strip()
    tok = secrets.token_urlsafe(24)
    p.write_text(tok + "\n")
    p.chmod(0o600)
    return tok


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packet is sent; picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def local_hosts() -> list[str]:
    """Names this machine can legitimately be reached by (for the Host-header allow-list)."""
    names = {"127.0.0.1", "localhost", "[::1]", socket.gethostname(), lan_ip()}
    try:
        names.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(names)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["http", "stdio"], default="http")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only; 0.0.0.0 = all interfaces)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allowed-host", action="append", default=[],
                    help="extra Host-header name clients may use (repeatable), e.g. a DNS alias of this PC")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    a = ap.parse_args()
    if not a.db.exists():
        sys.exit(f"database {a.db} not found — run: python -m rzmcp.build_fulltext && python -m rzmcp.build_registers")
    Q.set_db(a.db)
    mcp = build_server()
    if a.transport == "stdio":
        mcp.run("stdio")
        return
    import uvicorn
    token = load_token()
    hosts = local_hosts() + a.allowed_host
    # Two layers: Host/Origin allow-list (DNS-rebinding protection) + mandatory bearer token.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in hosts] + hosts,
        allowed_origins=[f"http://{h}:*" for h in hosts])
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security, host=a.host)
    ip = lan_ip() if a.host in ("0.0.0.0", "") else "localhost" if a.host in ("127.0.0.1", "::1") else a.host
    url = f"http://{ip}:{a.port}/mcp"
    print(f"rz-docs MCP server: {url}  (accepted Host names: {', '.join(hosts)})\n"
          f"Add it in Claude Code:\n"
          f"  claude mcp add --scope user --transport http rz-docs {url} --header \"Authorization: Bearer <token in data/.token>\"\n",
          file=sys.stderr)
    # a client that keeps a stream open must not hold a restart for systemd's 90 s stop timeout
    uvicorn.run(BearerAuth(app, token), host=a.host, port=a.port, log_level="info", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
