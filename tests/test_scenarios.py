"""Scripted conversations, one per seeded scenario.

Claude's side is scripted (FakeClaude), so these tests don't judge the model's
judgment. They check that each seeded scenario produces the data the model must
base its answer on, and that the bot's plumbing (customer memory, tool budget,
tickets, handoffs, failure handling) behaves end to end against the real mock
MCP server. The model's own behavior per scenario is checked in test_live.py.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from app.bot import MSG_HANDOFF_ACTIVE
from tests.helpers import outputs, resp, text, tool


def rows(db, sql):
    with sqlite3.connect(db) as c:
        return c.execute(sql).fetchall()


def checks(line_id, node_id, history=False):
    """The diagnosis round every scenario must do: line + node (+ history)."""
    calls = [tool("get_line_status", line_id=line_id), tool("get_node_status", node_id=node_id)]
    if history:
        calls.append(tool("get_signal_history", line_id=line_id, hours=24))
    return resp(*calls)


# --------------------------------------------------------------------------
async def test_area_outage(env):
    """CUS-005 on NODO-NORTE-01: line down because of active outage INC-2041."""
    bot, fake = env.make([
        resp(text("¡Hola! Para revisarlo, ¿me das tu teléfono o número de cuenta?")),
        resp(tool("find_customer", phone_or_account="600 111 005")),
        checks("LN-1005", "NODO-NORTE-01"),
        resp(text("Hay una avería en tu zona; la estimación es para las 00:40.")),
        resp(text("Según el sistema, la estimación sigue siendo la misma.")),
    ])
    r1 = await bot.handle_message("c", "Hola, no tengo internet")
    assert r1.tool_calls == []

    r2 = await bot.handle_message("c", "600 111 005")
    [cust] = outputs(r2, "find_customer")
    assert cust["customer"]["customer_id"] == "CUS-005"
    [line] = outputs(r2, "get_line_status")
    assert line["status"] == "offline" and line["signal_quality"] == "none"
    [node] = outputs(r2, "get_node_status")
    assert node["has_active_incident"] and node["affected_lines"] == node["total_lines"] == 3
    [inc] = node["active_incidents"]
    assert inc["incident_id"] == "INC-2041" and inc["type"] == "outage"
    assert datetime.fromisoformat(inc["eta"]) > datetime.now(timezone.utc)   # ETA is real data

    # Follow-up turn: customer + outage data ride along in context; no new lookups needed.
    r3 = await bot.handle_message("c", "¿Y para cuándo estará?")
    assert r3.tool_calls == []
    ctx = fake.chat_requests[-1]["messages"][-1]["content"]
    assert "CUS-005" in ctx and "INC-2041" in ctx and inc["eta"] in ctx
    assert rows(env.mock_db, "select count(*) from tickets") == [(0,)]       # known outage: no ticket


async def test_line_offline_customer_side(env):
    """CUS-003: line down ~5h, node healthy -> ticket + handoff for a technical visit."""
    bot, _ = env.make([
        resp(tool("find_customer", phone_or_account="ACC-50003")),
        checks("LN-1003", "NODO-CENTRO-02"),
        resp(tool("create_ticket", customer_id="CUS-003", severity="high",
                  summary="Línea LN-1003 caída ~5 h; nodo sin incidencias")),
        resp(tool("handoff_to_human", conversation_id="?", reason="Requiere visita técnica")),
        resp(text("He abierto el ticket y una persona coordinará la visita.")),
    ])
    r = await bot.handle_message("c", "Cuenta ACC-50003, sin internet desde esta mañana")
    [line] = outputs(r, "get_line_status")
    [node] = outputs(r, "get_node_status")
    assert line["status"] == "offline" and not node["has_active_incident"]
    down_for = datetime.now(timezone.utc) - datetime.fromisoformat(line["last_change_at"])
    assert 4.5 * 3600 < down_for.total_seconds() < 5.5 * 3600
    assert len(r.tool_calls) == 5 and r.status == "handoff"
    assert rows(env.mock_db, "select customer_id, severity from tickets") == [("CUS-003", "high")]
    assert rows(env.mock_db, "select conversation_id from handoffs") == [("c",)]

    r2 = await bot.handle_message("c", "¿Hola? ¿Sigue ahí?")
    assert r2.reply == MSG_HANDOFF_ACTIVE and r2.tool_calls == []


@pytest.mark.parametrize("phone,line_id,node_id,tech", [
    ("600111002", "LN-1002", "NODO-CENTRO-02", "FTTH"),
    ("600111009", "LN-1009", "NODO-SUR-03", "VDSL"),
])
async def test_weak_signal(env, phone, line_id, node_id, tech):
    """CUS-002 (fiber, low rx power) / CUS-009 (VDSL, low SNR): online but weak."""
    cust_id = "CUS-00" + phone[-1]
    bot, _ = env.make([
        resp(tool("find_customer", phone_or_account=phone)),
        checks(line_id, node_id, history=True),
        resp(tool("create_ticket", customer_id=cust_id, severity="medium",
                  summary="Señal débil en la línea")),
        resp(text("Tu señal llega más débil de lo normal; he abierto un ticket.")),
    ])
    r = await bot.handle_message("c", f"Soy el {phone}, internet me va lentísimo")
    [line] = outputs(r, "get_line_status")
    assert line["status"] == "online" and line["signal_quality"] == "weak"
    assert line["technology"] == tech
    if tech == "FTTH":
        assert line["rx_power_dbm"] < -25
    else:
        assert line["snr_db"] < 8
    [node] = outputs(r, "get_node_status")
    assert not node["has_active_incident"]
    [hist] = outputs(r, "get_signal_history")
    assert hist["summary"]["drops"] == 0 and hist["summary"]["avg_packet_loss_pct"] > 1
    assert rows(env.mock_db, "select customer_id, severity from tickets") == [(cust_id, "medium")]


async def test_intermittent_drops(env):
    """CUS-004: online right now, but repeated drops in the last 24 h."""
    bot, _ = env.make([
        resp(tool("find_customer", phone_or_account="+34 600 111 004")),
        checks("LN-1004", "NODO-CENTRO-02", history=True),
        resp(tool("create_ticket", customer_id="CUS-004", severity="medium",
                  summary="Cortes intermitentes: 15 caídas en 24 h")),
        resp(text("He visto cortes repetidos en tu línea; he abierto un ticket.")),
    ])
    r = await bot.handle_message("c", "Se me corta internet cada dos por tres. +34 600 111 004")
    [line] = outputs(r, "get_line_status")
    assert line["status"] == "online"                         # looks fine *right now*
    [hist] = outputs(r, "get_signal_history")
    s = hist["summary"]
    assert s["drops"] >= 10 and s["offline_minutes"] >= 150   # the history tells the real story
    assert any(x["status"] == "offline" for x in hist["samples"])
    assert len(r.tool_calls) == 5                             # 1 + 3 + 1: exactly the budget


async def test_all_good_line_troubleshoot_then_escalate(env):
    """CUS-001: healthy line, no incident -> basic troubleshooting; after 2 failed
    attempts -> ticket + handoff."""
    bot, fake = env.make([
        resp(tool("find_customer", phone_or_account="600111001")),
        checks("LN-1001", "NODO-CENTRO-02", history=True),
        resp(text("Tu línea está bien. Prueba a reiniciar el router (intento 1).")),
        resp(text("Vale. Prueba a conectarte por cable en lugar de wifi (intento 2).")),
        resp(tool("create_ticket", customer_id="CUS-001", severity="low",
                  summary="Lentitud con línea sana; 2 intentos sin éxito")),
        resp(tool("handoff_to_human", conversation_id="?", reason="Sin resolver tras 2 intentos")),
        resp(text("Te paso con una persona del equipo.")),
    ])
    r1 = await bot.handle_message("c", "600111001, me va lento el wifi")
    [line] = outputs(r1, "get_line_status")
    [node] = outputs(r1, "get_node_status")
    [hist] = outputs(r1, "get_signal_history")
    assert line["status"] == "online" and line["signal_quality"] == "good"
    assert not node["has_active_incident"] and hist["summary"]["drops"] == 0

    r2 = await bot.handle_message("c", "Lo he reiniciado y sigue igual")
    assert r2.tool_calls == []
    r3 = await bot.handle_message("c", "Por cable también va lento")
    assert [c["name"] for c in r3.tool_calls] == ["create_ticket", "handoff_to_human"]
    assert r3.status == "handoff"
    # The troubleshooting turns could see the healthy-line data without re-querying.
    assert '"signal_quality":"good"' in fake.chat_requests[3]["messages"][-1]["content"]


async def test_unknown_customer(env):
    """A phone/account not in the system: nothing is stored, the bot asks again."""
    bot, fake = env.make([
        resp(tool("find_customer", phone_or_account="699 000 000")),
        resp(text("No encuentro ese número. ¿Puedes revisarlo o darme tu número de cuenta?")),
        resp(tool("find_customer", phone_or_account="ACC-99999")),
        resp(text("Tampoco lo encuentro. ¿Quieres que te pase con una persona?")),
    ])
    r1 = await bot.handle_message("c", "Mi teléfono es 699 000 000")
    assert outputs(r1, "find_customer") == [{"found": False, "customer": None}]
    assert env.storage.get_or_create("c").customer is None
    r2 = await bot.handle_message("c", "Entonces mi cuenta es ACC-99999")
    assert outputs(r2, "find_customer") == [{"found": False, "customer": None}]
    turn2_first = fake.chat_requests[2]["messages"][-1]          # context message
    assert turn2_first["role"] == "system"
    assert "Cliente identificado: todavía no" in turn2_first["content"]
    assert len(fake.script) == 0


async def test_mcp_server_down(http_env):
    """The tracker goes down mid-conversation: tool calls fail fast as errors the
    model sees, the turn still completes, nothing crashes or hangs."""
    bot, fake = http_env.make([
        resp(tool("find_customer", phone_or_account="600111001")),
        resp(text("Gracias, María. ¿Qué problema tienes?")),
        checks("LN-1001", "NODO-CENTRO-02"),
        resp(text("Ahora mismo no puedo comprobar el estado de tu línea. Inténtalo más tarde.")),
    ])
    r1 = await bot.handle_message("c", "600111001")
    assert outputs(r1, "find_customer")[0]["found"]

    http_env.server.stop()
    t0 = datetime.now()
    r2 = await bot.handle_message("c", "No tengo internet")
    assert (datetime.now() - t0).total_seconds() < 5          # fails fast, no hang
    assert [c["is_error"] for c in r2.tool_calls] == [True, True]
    assert all("Tool unavailable" in c["output"] for c in r2.tool_calls)
    sent = fake.chat_requests[-1]["messages"][-1]["content"]  # what the model saw
    assert all(b["is_error"] and "Tool unavailable" in b["content"] for b in sent)
    assert r2.reply.startswith("Ahora mismo no puedo") and r2.status == "active"
    logged = rows(http_env.storage.path, "select is_error from tool_calls order by id")
    assert logged == [(0,), (1,), (1,)]
