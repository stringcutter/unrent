"""Start `unrent mcp` from an installed wheel and call every tool over stdio.

Usage: python scripts/mcp_smoke.py <directory to scan>
Run it in an environment with `unrent[mcp]` installed, outside the checkout.
"""

from __future__ import annotations

import os
import sys

import anyio
from mcp import Client, StdioServerParameters


async def main(target: str) -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "unrent.cli", "mcp"], env=dict(os.environ)
    )
    async with Client(params) as client:
        tools = sorted(t.name for t in (await client.list_tools()).tools)
        assert tools == ["alternatives", "catalog", "scan", "standing"], tools
        calls = {
            "scan": {"path": target},
            "alternatives": {"query": "pinecone"},
            "standing": {"repo": "qdrant/qdrant"},
            "catalog": {},
        }
        results = {}
        for name, args in calls.items():
            result = await client.call_tool(name, args)
            assert not result.is_error, (name, result.content)
            results[name] = result.structured_content
            print(f"{name}: ok, rankings {results[name]['rankings']}")
    found = [f["id"] for f in results["scan"]["found"]]
    assert "openai" in found, found
    return 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main, sys.argv[1]))
