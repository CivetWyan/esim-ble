"""MCP smoke test: spawn `python -m esimble.platform mcp` over stdio like an AI client would,
list the tools and call the read-only ones.

    .venv/bin/python scripts/mcp_smoke.py [number]

Needs the platform (`python -m esimble.platform serve`) to be running.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


def text(r) -> str:
    if getattr(r, "structured_content", None) is not None:
        return json.dumps(r.structured_content, ensure_ascii=False)[:600]
    return " ".join(getattr(c, "text", "") for c in r.content)[:600]


async def main(number: str | None) -> None:
    params = StdioServerParameters(command=str(ROOT / ".venv/bin/python"),
                                   args=["-m", "esimble.platform", "mcp"], cwd=str(ROOT))
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as s:
            init = await s.initialize()
            print("server:", init.server_info.name)
            tools = await s.list_tools()
            print("tools:", [t.name for t in tools.tools])
            print("get_status ->", text(await s.call_tool("get_status", {})))
            print("list_numbers ->", text(await s.call_tool("list_numbers", {})))
            print("list_messages ->", text(await s.call_tool("list_messages", {"limit": 3})))
            if number:
                print("get_latest_code ->", text(await s.call_tool(
                    "get_latest_code", {"number": number, "max_age_seconds": 10 ** 7})))
                print("wait_for_sms(5s) ->", text(await s.call_tool(
                    "wait_for_sms", {"number": number, "timeout_seconds": 5})))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
