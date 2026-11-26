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
                args = {a.name: a.required for a in (p.arguments or [])} if p else {}
                check(list(args) == ["name", "project"] and args["name"] and not args["project"],
                      "arguments: required 'name', optional 'project'")
                def text(res): return "\n".join(m.content.text for m in res.messages)
                t = text(await s.get_prompt("poc", {"name": "lowpower-display"}))
                check("不是用 poc-new 创建的" in t, "real delivered repo rzg3l-lowpower-display -> stop, do not write")
                t = text(await s.get_prompt("poc", {"name": "e2e-new-poc"}))
                check("创建 PoC 仓库" in t and "/rzg3l-bsp-v1/rzg3l-e2e-new-poc" in t, "new name -> create step under rzg3l-bsp-v1")
                import subprocess
                ia = subprocess.run(["python3", "/home/mpu-epmaj/max/workspace/rz-mcp/workflow/install-agents", "--check"], capture_output=True)
                check(ia.returncode == 0, "agents installed in rzg3l-bsp-v1 are current (install-agents --check)")
                t = text(await s.get_prompt("poc", {"name": "e2e-new-poc", "project": "/home/mpu-epmaj/max/.rz-mcp-e2e/none"}))
                check("准备 Yocto 项目（第 0 步）" in t and "yocto-project setup /home/mpu-epmaj/max/.rz-mcp-e2e/none" in t,
                      "a new project directory -> step 0 (set it up)")
                t = text(await s.get_prompt("poc", {"name": "e2e-new-poc"}))
                check("/home/mpu-epmaj/max/.rz-mcp-e2e/none" in t, "the directory is remembered for the PoC")
                res = await s.call_tool("check_build_env", {"project": "/home/mpu-epmaj/max/workspace/rzg3l-bsp-v1"})
                check(not res.is_error and "project state: usable" in res.content[0].text, "check_build_env takes a project")
                res = await s.call_tool("list_documents", {"doc_type": "hw_manual"})
                check("R01UH1152EJ0110" in res.content[0].text and "CONFIDENTIAL, not indexed" in res.content[0].text,
                      "the confidential HW manual is listed, flagged, not indexed")
                res = await s.call_tool("get_toc", {})
                check("R01UH1153EJ0110" in res.content[0].text.splitlines()[0], "the default HW manual stays the public edition")
                res = await s.call_tool("lookup_register", {"query": "WDTSET_2"})
                check(not res.is_error and "WDTSET" in res.content[0].text, "existing tool still works (lookup_register)")
asyncio.run(main())
# the service registered the test PoC name in the real registry: take it out again
import json, pathlib
reg = pathlib.Path("/home/mpu-epmaj/max/workspace/rz-mcp/data/poc-projects.json")
if reg.exists():
    d = json.loads(reg.read_text()); d.pop("e2e-new-poc", None); reg.write_text(json.dumps(d, indent=1))
print(f"\n{len(fails)} failed"); sys.exit(1 if fails else 0)
