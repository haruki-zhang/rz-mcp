#!/usr/bin/env python3
"""Extract every tool call of a PoC run (main session + subagents) with its result, in order, to calls.jsonl.

    python3 eval/gap/extract.py <session.jsonl> <out.jsonl>

Each line: {agent, i, ts, tool, input, result (text, max 6000 chars), result_len, is_error}. The agent is "main" or
the subagent's description (from <session>/subagents/*.meta.json).
"""
import json
import sys
from pathlib import Path


def text_of(c) -> str:
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")


def calls(path: Path, agent: str) -> list[dict]:
    uses, out = {}, []
    for line in open(path, encoding="utf-8"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("isSidechain") and agent == "main":
            continue
        m = o.get("message") or {}
        if o.get("type") == "assistant":
            for b in m.get("content", []):
                if b.get("type") == "tool_use":
                    rec = {"agent": agent, "ts": o["timestamp"], "tool": b["name"].replace("mcp__rz-docs__", "mcp:"),
                           "input": b["input"]}
                    uses[b["id"]] = rec
                    out.append(rec)
        elif o.get("type") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                    t = text_of(b.get("content"))
                    r = uses[b["tool_use_id"]]
                    r.update(result=t[:6000], result_len=len(t), is_error=bool(b.get("is_error")))
    for i, r in enumerate(out):
        r["i"] = i
    return out


def main() -> None:
    session, dest = Path(sys.argv[1]), Path(sys.argv[2])
    allc = calls(session, "main")
    sub = session.with_suffix("") / "subagents"
    for f in sorted(sub.glob("*.jsonl")):
        meta = json.loads(f.with_suffix(".meta.json").read_text())
        allc += calls(f, f"{meta['agentType']}: {meta['description']}")
    with open(dest, "w", encoding="utf-8") as f:
        for r in allc:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(allc)} calls -> {dest}")


if __name__ == "__main__":
    main()
