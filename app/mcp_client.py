"""Transport-agnostic MCP access. Targets are pure config (see MCP_SIGNAL_SERVER).

- MCPConnection: one server; `session()` plus one-shot helpers (compare_tools.py).
- MCPToolbox: merged tool list (Anthropic format) from one or more servers and
  name-based routing; one short session per call, with a timeout.
"""
import json
import logging
import os
import shlex
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def _is_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


class MCPConnection:
    def __init__(self, target: str, token: str = "", timeout: float = 30.0):
        if not target.strip():
            raise ValueError("MCP server target is empty")
        self.target = target.strip()
        self.token = token
        self.timeout = timeout

    def _server(self) -> Any:
        if _is_url(self.target):
            if not self.token:
                return self.target
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.token}"})
            return streamable_http_client(self.target, http_client=http)
        cmd, *args = shlex.split(self.target)
        if cmd == "python":  # run with this interpreter/venv
            cmd = sys.executable
        # The SDK only passes a minimal safe env to subprocesses; forward ours by prefix
        # (not everything, so e.g. ANTHROPIC_API_KEY doesn't leak into the server).
        env = {k: v for k, v in os.environ.items() if k.startswith(("MOCK_", "MCP_"))}
        return StdioServerParameters(command=cmd, args=args, cwd=ROOT, env=env)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Client]:
        async with Client(self._server(), read_timeout_seconds=self.timeout) as client:
            yield client

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self.session() as c:
            result = await c.list_tools()
        return [t.model_dump(by_alias=True, exclude_none=True) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self.session() as c:
            result = await c.call_tool(name, arguments)
        return result.model_dump(by_alias=True, exclude_none=True)


@dataclass
class ToolResult:
    text: str                       # what the model sees
    is_error: bool
    structured: dict[str, Any] | None


class MCPToolbox:
    """Merged tool list from one or more MCP servers + name-based call routing.

    Each tool call opens its own short-lived session under a hard timeout. A
    long-lived shared session was tried first and rejected: when the server dies,
    the MCP client cancels the task that owns the session (here, the app itself)
    and closing it hangs. Per-call sessions isolate failures completely; the cost
    is ~50 ms per call over HTTP (~0.4 s over stdio, which spawns a process).
    """

    def __init__(self, connections: list[MCPConnection], call_timeout: float = 15.0):
        # Dedupe by target so signal + customer on the same server are listed once.
        seen: dict[str, MCPConnection] = {}
        for c in connections:
            seen.setdefault(c.target, c)
        self.connections = list(seen.values())
        self.call_timeout = call_timeout
        self._routes: dict[str, MCPConnection] = {}
        self._tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> "MCPToolbox":
        """Load tool definitions once (fails fast if a server is unreachable)."""
        collected: dict[str, dict[str, Any]] = {}
        for conn in self.connections:
            with anyio.fail_after(self.call_timeout):
                async with conn.session() as client:
                    cursor = None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        for t in page.tools:
                            if t.name in self._routes:
                                log.warning("Tool %s exposed by several servers; keeping the first",
                                            t.name)
                                continue
                            self._routes[t.name] = conn
                            collected[t.name] = {
                                "name": t.name,
                                "description": t.description or "",
                                "input_schema": t.input_schema,
                            }
                        cursor = page.next_cursor
                        if not cursor:
                            break
        # Sorted + built once => byte-stable tools array => cacheable prefix.
        self._tools = [collected[n] for n in sorted(collected)]
        log.info("MCP tools loaded: %s", ", ".join(collected))
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    @property
    def tool_names(self) -> list[str]:
        return [t["name"] for t in self._tools]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """Tool definitions for the Messages API, cache breakpoint on the last one."""
        tools = [dict(t) for t in self._tools]
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        conn = self._routes.get(name)
        if conn is None:
            return ToolResult(f"Unknown tool: {name}", True, None)
        try:
            with anyio.fail_after(self.call_timeout):
                async with conn.session() as client:
                    r = await client.call_tool(name, arguments)
        except TimeoutError:
            log.error("MCP call %s timed out after %ss", name, self.call_timeout)
            return ToolResult("Tool unavailable: the signal tracker did not respond in time.",
                              True, None)
        except Exception:  # unreachable/crashed server: report to the model, keep the turn alive
            log.exception("MCP call %s failed", name)
            return ToolResult("Tool unavailable: could not reach the signal tracker.", True, None)
        if r.structured_content is not None and not r.is_error:
            text = json.dumps(r.structured_content, ensure_ascii=False, separators=(",", ":"))
        else:
            text = "\n".join(getattr(b, "text", "") for b in r.content) or "(empty result)"
        return ToolResult(text, bool(r.is_error), r.structured_content)
