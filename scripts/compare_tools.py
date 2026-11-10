"""Compare tool names + input/output schemas of two MCP servers (mock vs real).

Usage:
  uv run python scripts/compare_tools.py "python -m mock_mcp.server" https://real/mcp
Targets use the same format as MCP_SIGNAL_SERVER. Mock-only customer tools are
reported but not counted as mismatches.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.mcp_client import MCPConnection  # noqa: E402

MOCK_ONLY = {"find_customer", "create_ticket", "handoff_to_human"}


async def main(mock: str, real: str) -> int:
    m = {t["name"]: t for t in await MCPConnection(mock).list_tools()}
    r = {t["name"]: t for t in await MCPConnection(real).list_tools()}
    bad = 0
    for name in sorted(r.keys() - m.keys()):
        print(f"MISSING in mock: {name}"); bad += 1
    for name in sorted(m.keys() - r.keys()):
        print(f"{'mock-only' if name in MOCK_ONLY else 'EXTRA in mock'}: {name}")
        bad += name not in MOCK_ONLY
    for name in sorted(m.keys() & r.keys()):
        for key in ("inputSchema", "outputSchema"):
            if m[name].get(key) != r[name].get(key):
                bad += 1
                print(f"DIFF {name}.{key}\n  mock: {json.dumps(m[name].get(key))}\n  real: {json.dumps(r[name].get(key))}")
    print("OK – schemas match" if not bad else f"{bad} mismatch(es)")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
