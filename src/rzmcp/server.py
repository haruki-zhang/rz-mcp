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
    uvicorn.run(BearerAuth(app, token), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
