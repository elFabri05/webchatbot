"""Bot loop tests: scripted fake Claude client + real mock MCP server (stdio).
No API calls are made. Fixtures live in conftest.py."""
import sqlite3

from app.bot import MSG_HANDOFF_ACTIVE
from tests.helpers import resp, text, tool


async def test_simple_turn_and_cache_layout(env):
    bot, fake = env.make([resp(text("¡Hola! ¿Me das tu teléfono?"))])
    r = await bot.handle_message("c1", "hola, no tengo internet")
    assert r.reply.startswith("¡Hola!")
    req = fake.chat_requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert sum("cache_control" in t for t in req["tools"]) == 1
    assert [t["name"] for t in req["tools"]] == sorted(t["name"] for t in req["tools"])
    # Per-conversation context goes after the history, as a system message.
    assert req["messages"][0] == {"role": "user", "content": "hola, no tengo internet"}
    assert req["messages"][-1]["role"] == "system"
    assert "conversation_id: c1" in req["messages"][-1]["content"]


async def test_system_and_tools_byte_stable_across_turns(env):
    bot, fake = env.make([resp(text("a")), resp(text("b"))])
    await bot.handle_message("c1", "uno")
    await bot.handle_message("c1", "dos")
    a, b = fake.chat_requests
    assert a["system"] == b["system"] and a["tools"] == b["tools"]
    # Second turn replays the first as plain text.
    assert b["messages"][:3] == [{"role": "user", "content": "uno"},
                                 {"role": "assistant", "content": "a"},
                                 {"role": "user", "content": "dos"}]


async def test_tool_loop_identifies_customer_and_checks_line(env):
    bot, fake = env.make([
        resp(tool("find_customer", phone_or_account="600111005")),
        resp(tool("get_line_status", line_id="LN-1005"),
             tool("get_node_status", node_id="NODO-NORTE-01")),
        resp(text("Hay una avería en tu zona.")),
    ])
    r = await bot.handle_message("c1", "soy el 600111005, no tengo internet")
    assert [c["name"] for c in r.tool_calls] == ["find_customer", "get_line_status",
                                                 "get_node_status"]
    assert not any(c["is_error"] for c in r.tool_calls)
    conv = env.storage.get_or_create("c1")
    assert conv.customer["customer_id"] == "CUS-005"

    last = fake.chat_requests[-1]["messages"]
    results = last[-1]["content"]
    assert [x["type"] for x in results] == ["tool_result", "tool_result"]
    assert '"has_active_incident":true' in results[1]["content"]
    # Exactly one moving message breakpoint (+ tools + system = 3 <= 4).
    n = sum(1 for m in last if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if "cache_control" in b)
    assert n == 1 and "cache_control" in results[-1]

    # Next turn: customer and tool data are in the context; no need to look up again.
    bot2, fake2 = env.make([resp(text("ok"))])
    await bot2.handle_message("c1", "¿y cuándo vuelve?")
    ctx = fake2.chat_requests[0]["messages"][-1]["content"]
    assert "CUS-005" in ctx and "INC-2041" in ctx


async def test_tool_budget_enforced(env):
    calls = [tool("get_line_status", line_id=f"LN-100{i}") for i in range(1, 8)]
    bot, fake = env.make([
        resp(*calls[:3]),
        resp(*calls[3:7]),             # 4 more requested; only 2 left in budget
        resp(text("Respuesta final")),
    ])
    r = await bot.handle_message("c1", "revisa todo")
    assert len(r.tool_calls) == 5
    second = fake.chat_requests[2]
    denied = [b for b in second["messages"][-1]["content"] if b["is_error"]]
    assert len(denied) == 2 and "limit of 5" in denied[0]["content"]
    assert second["tool_choice"] == {"type": "none"}
    assert r.reply == "Respuesta final"


async def test_handoff_overrides_conversation_id_and_locks_conversation(env):
    bot, fake = env.make([
        resp(tool("handoff_to_human", conversation_id="whatever", reason="Cliente enfadado")),
        resp(text("Te paso con una persona.")),
    ])
    r = await bot.handle_message("conv-42", "¡esto es un desastre!")
    assert r.status == "handoff"
    with sqlite3.connect(env.mock_db) as c:
        assert c.execute("select conversation_id from handoffs").fetchall() == [("conv-42",)]
    r2 = await bot.handle_message("conv-42", "¿hola?")
    assert r2.reply == MSG_HANDOFF_ACTIVE and len(fake.requests) == 2   # no model call


async def test_window_and_running_summary(env):
    bot, fake = env.make([resp(text(f"r{i}")) for i in range(8)], history_window=4)
    for i in range(8):
        await bot.handle_message("c1", f"m{i}")
    summaries = [r for r in fake.requests if isinstance(r["system"], str)]
    assert summaries, "summary should run once history exceeds the window"
    last = fake.chat_requests[-1]["messages"]
    history = last[:-1]
    assert len(history) <= 4 and history[0]["role"] == "user"
    assert history[-1] == {"role": "user", "content": "m7"}
    assert "RESUMEN" in last[-1]["content"]
    conv = env.storage.get_or_create("c1")
    assert conv.summary == "RESUMEN" and conv.summarized_through > 0
    assert len(env.storage.all_messages("c1")) == 16          # nothing deleted


async def test_refusal_and_api_error(env):
    import anthropic
    import httpx2

    bot, fake = env.make([resp(stop="refusal")])
    r = await bot.handle_message("c1", "x")
    assert "no puedo ayudarte" in r.reply

    async def boom(**kw):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    bot2, fake2 = env.make([])
    fake2.beta.messages.create = boom
    r2 = await bot2.handle_message("c2", "x")
    assert "problema técnico" in r2.reply
