# Run: .venv/bin/python eval/e2e_workflow.py    (needs the rz-mcp service running on localhost:8765)
"""End-to-end over HTTP against the running rz-docs service: prompt listed, stage logic on real repos, tools still fine."""
import asyncio, sys
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

TOKEN = open("/home/mpu-epmaj/max/workspace/rz-mcp/data/.token").read().strip()
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

async def main():
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as hc:
        async with streamable_http_client("http://localhost:8765/mcp", http_client=hc) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                print("protocol:", getattr(init, "protocol_version", "?"))
                ps = (await s.list_prompts()).prompts
                p = next((x for x in ps if x.name == "poc"), None)
                check(p is not None, "prompt 'poc' is listed")
                check(p and [a.name for a in (p.arguments or [])] == ["name"] and p.arguments[0].required, "single required argument 'name'")
                def text(res): return "\n".join(m.content.text for m in res.messages)
                t = text(await s.get_prompt("poc", {"name": "lowpower-display"}))
                check("不是用 poc-new 创建的" in t, "real delivered repo rzg3l-lowpower-display -> stop, do not write")
                t = text(await s.get_prompt("poc", {"name": "e2e-new-poc"}))
                check("创建 PoC 仓库" in t and "/rzg3l-bsp-v1/rzg3l-e2e-new-poc" in t, "new name -> create step under rzg3l-bsp-v1")
                check("install-agents" not in t, "agents installed in rzg3l-bsp-v1 are current")
                res = await s.call_tool("lookup_register", {"query": "WDTSET_2"})
                check(not res.is_error and "WDTSET" in res.content[0].text, "existing tool still works (lookup_register)")
asyncio.run(main())
print(f"\n{len(fails)} failed"); sys.exit(1 if fails else 0)
