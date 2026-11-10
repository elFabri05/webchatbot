"""HTTP tests: real app + lifespan (mock MCP over stdio), fake Claude client."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.storage import Storage
from tests.helpers import FakeClaude, resp, text, tool


@pytest.fixture
def client(tmp_path, monkeypatch, mock_db):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")      # never export from tests
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def script(c, *responses):
    fake = FakeClaude(responses)
    c.app.state.bot.client = fake
    return fake


def test_pages_served(client):
    for path in ("/", "/admin"):
        r = client.get(path)
        assert r.status_code == 200 and "<!doctype html>" in r.text


def test_chat_then_admin_overview(client):
    script(client,
           resp(tool("find_customer", phone_or_account="600111003")),
           resp(tool("get_line_status", line_id="LN-1003"),
                tool("get_node_status", node_id="NODO-CENTRO-02")),
           resp(tool("create_ticket", customer_id="CUS-003", summary="Línea caída", severity="high")),
           resp(tool("handoff_to_human", conversation_id="x", reason="Visita técnica")),
           resp(text("Te paso con un técnico.")))
    r = client.post("/chat", json={"message": "soy el 600111003, no tengo internet"})
    assert r.status_code == 200
    body = r.json()
    conv = body["conversation_id"]
    assert body["status"] == "handoff" and len(body["tool_calls"]) == 5
    assert body["usage"]["input_tokens"] == 50          # 5 fake API calls x 10

    # Transcript: tool calls are linked to the assistant message that closed the turn.
    t = client.get(f"/api/conversations/{conv}").json()
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"]
    assert len(t["messages"][1]["tool_calls"]) == 5
    assert t["messages"][1]["usage"]["output_tokens"] == 25

    o = client.get("/admin/api/overview").json()
    assert o["totals"]["conversations"] == 1 and o["totals"]["handoffs"] == 1
    assert o["totals"]["input_tokens"] == 50
    [ticket] = o["tickets"]
    assert ticket["severity"] == "high" and ticket["conversation_id"] == conv
    [handoff] = o["handoffs"]
    assert handoff["conversation_id"] == conv and handoff["reason"] == "Visita técnica"
    [row] = o["conversations"]
    assert row["customer"]["customer_id"] == "CUS-003" and row["tool_calls"] == 5


def test_chat_debug_off_hides_tool_data(client, monkeypatch):
    monkeypatch.setenv("CHAT_DEBUG", "false")
    get_settings.cache_clear()
    script(client, resp(tool("find_customer", phone_or_account="600111001")), resp(text("Hola María")))
    body = client.post("/chat", json={"message": "600111001"}).json()
    assert body["tool_calls"] == [] and body["usage"] == {}
    t = client.get(f"/api/conversations/{body['conversation_id']}").json()
    assert t["messages"][1]["tool_calls"] == []


def test_storage_migrates_old_schema(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:   # first-release schema, without usage columns
        c.executescript("""
            CREATE TABLE messages (message_id INTEGER PRIMARY KEY AUTOINCREMENT,
              conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
              tool_notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
              name TEXT NOT NULL, input_json TEXT NOT NULL, output TEXT NOT NULL, is_error INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT '');""")
    s = Storage(db)
    s.get_or_create("c")
    s.add_message("c", "assistant", "hola", usage={"input_tokens": 7})
    assert s.transcript("c")[0]["usage"]["input_tokens"] == 7
