import sys
from pathlib import Path

import pytest
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from reset_db import reset  # noqa: E402

from app.mcp_client import MCPConnection
from mock_mcp.server import mcp as server


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    reset(db)
    monkeypatch.setenv("MOCK_DB_PATH", str(db))
    return db


async def call(name, **args):
    async with Client(server) as c:
        r = await c.call_tool(name, args)
    return r


async def test_tools_listed():
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"get_line_status", "get_signal_history", "get_node_status",
            "find_customer", "create_ticket", "handoff_to_human"} <= names


@pytest.mark.parametrize("query", ["+34600111004", "600 111 004", "acc-50004"])
async def test_find_customer(query):
    r = (await call("find_customer", phone_or_account=query)).structured_content
    assert r["found"] and r["customer"]["line_id"] == "LN-1004"


async def test_find_customer_missing():
    r = (await call("find_customer", phone_or_account="999")).structured_content
    assert r == {"found": False, "customer": None}


@pytest.mark.parametrize("line,status,quality", [
    ("LN-1001", "online", "good"),
    ("LN-1002", "online", "weak"),
    ("LN-1003", "offline", "none"),
    ("LN-1009", "online", "weak"),
])
async def test_line_states(line, status, quality):
    r = (await call("get_line_status", line_id=line)).structured_content
    assert (r["status"], r["signal_quality"]) == (status, quality)


async def test_intermittent_line_has_drops():
    r = (await call("get_signal_history", line_id="LN-1004")).structured_content
    assert r["summary"]["drops"] >= 5
    stable = (await call("get_signal_history", line_id="LN-1001")).structured_content
    assert stable["summary"]["drops"] == 0


async def test_node_outage_and_clean_node():
    norte = (await call("get_node_status", node_id="NODO-NORTE-01")).structured_content
    assert norte["has_active_incident"] and norte["active_incidents"][0]["eta"]
    centro = (await call("get_node_status", node_id="NODO-CENTRO-02")).structured_content
    assert not centro["has_active_incident"]


async def test_ticket_and_handoff_written(fresh_db):
    import sqlite3
    await call("create_ticket", customer_id="CUS-003", summary="Sin conexión", severity="high")
    await call("handoff_to_human", conversation_id="c1", reason="Cliente molesto")
    with sqlite3.connect(fresh_db) as conn:
        assert conn.execute("select count(*) from tickets").fetchone()[0] == 1
        assert conn.execute("select count(*) from handoffs").fetchone()[0] == 1


async def test_errors_are_readable():
    r = await call("get_line_status", line_id="NOPE")
    assert r.is_error and "Unknown line_id" in r.content[0].text
    r = await call("create_ticket", customer_id="CUS-001", summary="x", severity="urgent")
    assert r.is_error


async def test_stdio_via_config_string():
    """Same path the app uses: MCP_SIGNAL_SERVER as a command line."""
    r = await MCPConnection("python -m mock_mcp.server").call_tool(
        "get_line_status", {"line_id": "LN-1005"})
    assert r["structuredContent"]["status"] == "offline"
