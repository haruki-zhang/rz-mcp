"""rz-dev MCP server: RZ/G3L documentation lookup plus the Yocto, PoC, board and job tools (devtools.py).

    # localhost (Streamable HTTP, bearer-token protected) — default; URL http://localhost:8765/mcp
    python -m rzmcp.server --transport http --port 8765
    # LAN (only if the MCP allowlist permits non-localhost URLs)
    python -m rzmcp.server --transport http --host 0.0.0.0 --port 8765
    # local (stdio) — for Claude Code on this PC
    python -m rzmcp.server --transport stdio

Token: $RZ_MCP_TOKEN, else data/.token (created on first start, mode 600).
Every tool call is appended to data/calls.jsonl (or --call-log) (tool, args, size, duration) for later review.
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
from . import devtools
from . import query as Q

SERVER_NAME = "rz-dev"
LOG_PATH = ROOT / "data" / "calls.jsonl"

INSTRUCTIONS = """\
rz-dev: Renesas RZ/G3L development server on this host: documentation lookup, the Yocto build environment, \
PoC projects, and flashing and verifying the RZ/G3L EVK board. 瑞萨 RZ/G3L 开发：查资料、Yocto 构建、PoC、烧录和上板验证。

Pick the tool by what the user wants:
- Chip or board facts (registers, pins, boot modes, electrical values, procedures, Linux drivers; 查资料、寄存器、\
手册): list_documents, search_docs, get_toc, get_section, read_pages, lookup_register, search_registers.
- Yocto / BSP environment, where the kernel / U-Boot / TF-A sources are (搭环境、Yocto、BSP、源码在哪): yocto_status; \
yocto_setup if it is missing or broken or an optional component is needed. Read BSP sources directly with your \
file tools at the paths yocto_status returns.
- Build an image (构建、编译、bitbake): poc_build.
- Flash the EVK (烧录、烧写、写板、刷机、flash、eMMC、QSPI、bootloader、rootfs): board_flash. Board state or \
connection (板子状态): board_status. One command on the board in U-Boot or Linux: board_exec. board_flash, \
board_verify and board_exec check the host and the board themselves; call them directly. board_flash without \
an image path flashes the newest built image.
- Verify or test on the board (上板验证、测试): board_verify.
- PoC / proof of concept / demo (概念验证、演示): poc_create to start one, poc_status to see where a PoC is and \
which tool comes next, poc_package for the delivery package.
- yocto_setup, poc_build, poc_package, board_flash and board_verify are background jobs: they return a job_id at \
once; call job_status(job_id, wait_seconds=50) again and again until the job ends. job_cancel stops a job.

Rules:
- When a job is waiting_human, tell the user its instruction word for word (a DIP switch or power operation), \
then keep polling job_status; the user does not reply, the tool detects the operation on the serial console.
- Every tool works on its own; pass PoC names or absolute paths. Each result says which tool to call next.
- PoC flow: yocto_status -> poc_create, research, write the design and verification plan -> the user confirms the \
design (the only decision point) -> implement in the PoC layer, poc_build -> board_flash, board_verify -> write \
the docs, poc_package.
- Documents are English: search with English keywords and register/module abbreviations. Cite doc_id, Rev and \
page numbers (PDF page indices) from the tool output. Structured registers cover WDT, SCIFA, CPG; for other \
modules use search_docs and get_section. Pin multiplexing (which Function number a signal has on a pin, ball \
location, reset state, pins that must be used together): search_docs with the pin or signal name, e.g. \
'PM4 RSCI0'; the hits are records of the pin function list, cite them by doc_id and pin. If something is not found, say so; never fill in values from memory.
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
    mcp = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, version="0.2.0")
    # plain-text results only (no structuredContent wrapper)
    ro = {"annotations": ToolAnnotations(read_only_hint=True, open_world_hint=False), "structured_output": False}

    @mcp.tool(**ro)
    @_logged
    def list_documents(device: DeviceArg = "rzg3l",
                       doc_type: Annotated[Optional[str], Field(description="Optional filter: hw_manual, datasheet, app_note, quick_start, board_manual, schematic, bom, errata, sw_manual, component_datasheet, pin_list")] = None) -> str:
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
        """Full-text search over all indexed documents (manual sections, schematic sheets, BOM rows, pin
        function list records). Returns ranked hits with doc_id, revision, section number, page and a snippet;
        BOM rows and pin records are shown in full. Read section/page hits with get_section or read_pages
        before answering; snippets alone are not enough to quote values."""
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

    devtools.register(mcp, _logged)
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
    global LOG_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["http", "stdio"], default="http")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only; 0.0.0.0 = all interfaces)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allowed-host", action="append", default=[],
                    help="extra Host-header name clients may use (repeatable), e.g. a DNS alias of this PC")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--call-log", type=Path, default=LOG_PATH, help="where tool calls are logged (JSON lines)")
    a = ap.parse_args()
    if not a.db.exists():
        sys.exit(f"database {a.db} not found — run: python -m rzmcp.build_fulltext && python -m rzmcp.build_registers")
    LOG_PATH = a.call_log
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
    print(f"{SERVER_NAME} MCP server: {url}  (accepted Host names: {', '.join(hosts)})\n"
          f"Add it in Claude Code:\n"
          f"  claude mcp add --scope user --transport http {SERVER_NAME} {url} --header \"Authorization: Bearer <token in data/.token>\"\n",
          file=sys.stderr)
    # a client that keeps a stream open must not hold a restart for systemd's 90 s stop timeout
    uvicorn.run(BearerAuth(app, token), host=a.host, port=a.port, log_level="info", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
