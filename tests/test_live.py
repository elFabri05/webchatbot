"""Live scenario tests: the REAL model (Claude API) against the mock MCP server.

These check the bot's behavior per seeded scenario: does it check line + node
before diagnosing, escalate when it should, avoid troubleshooting a broken
line, report the real ETA, and not invent data when the tracker is down.
Assertions are structural (which tools ran, what was written) plus a few
lenient text checks, so they tolerate normal wording variation.

Skipped unless RUN_LIVE_TESTS=1 (they cost money: a few dozen API calls with
the configured BOT_MODEL). Run with `make test-live`.
"""
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import pytest

from app.bot import BotConfig, ChatBot
from app.config import get_settings
from app.mcp_client import MCPConnection, MCPToolbox
from app.storage import Storage

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1",
                                reason="live API test; set RUN_LIVE_TESTS=1 (costs money)")


class Live:
    def __init__(self, bot: ChatBot, mock_db):
        self.bot, self.mock_db, self.log = bot, mock_db, []
        self.results = []

    async def say(self, conv: str, msg: str):
        r = await self.bot.handle_message(conv, msg)
        self.results.append(r)
        self.log.append(f"CLIENTE: {msg}\nBOT [{r.status}] tools={[c['name'] for c in r.tool_calls]}"
                        f"\n  {r.reply}")
        return r

    def tools(self) -> list[str]:
        return [c["name"] for r in self.results for c in r.tool_calls]

    def ok_tools(self) -> list[str]:
        return [c["name"] for r in self.results for c in r.tool_calls if not c["is_error"]]

    @property
    def transcript(self) -> str:
        return "\n".join(self.log)


async def _live(tmp_path, target: str) -> tuple[ChatBot, Storage]:
    s = get_settings()
    toolbox = await MCPToolbox([MCPConnection(target)], call_timeout=10).__aenter__()
    storage = Storage(tmp_path / "app.db")
    bot = ChatBot(anthropic.AsyncAnthropic(api_key=s.anthropic_api_key or None), toolbox, storage,
                  BotConfig(model=s.bot_model, effort=s.bot_effort, timezone=s.bot_timezone,
                            max_tool_calls=s.bot_max_tool_calls, use_fallbacks=s.bot_fallbacks))
    return bot, storage


@pytest.fixture
async def live(tmp_path, mock_db):
    bot, _ = await _live(tmp_path, "python -m mock_mcp.server")
    return Live(bot, mock_db)


def assert_checked_network(lv: Live):
    t = lv.ok_tools()
    assert "get_line_status" in t and "get_node_status" in t, lv.transcript


# --------------------------------------------------------------------------
async def test_live_area_outage(live, mock_db):
    import sqlite3
    with sqlite3.connect(mock_db) as c:
        (eta,) = c.execute("select eta from incidents where status='active'").fetchone()
    local = datetime.fromisoformat(eta).astimezone(ZoneInfo(get_settings().bot_timezone))

    await live.say("c", "Hola, no tengo internet desde hace un rato")
    r = await live.say("c", "Mi teléfono es 600111005")
    assert_checked_network(live)
    assert "handoff_to_human" not in live.tools(), live.transcript
    # Reports the real ETA, converted to local time. Rounding for the customer
    # ("hacia las 01:30" for 01:31) is fine; a time far from the ETA would be invented.
    eta_min = local.hour * 60 + local.minute
    said = [int(h) * 60 + int(m) for h, m in
            re.findall(r"\b(\d{1,2})[:.h](\d{2})\b", " ".join(x.reply for x in live.results))]
    assert any(min(abs(t - eta_min), 1440 - abs(t - eta_min)) <= 15 for t in said), \
        f"ETA {local:%H:%M} not reported (times said: {said})\n{live.transcript}"
    assert r.status == "active"


async def test_live_line_offline(live):
    await live.say("c", "Buenas, soy la cuenta ACC-50003")
    r = await live.say("c", "No tengo internet desde esta mañana, ya reinicié el router")
    assert_checked_network(live)
    assert r.status == "handoff", live.transcript          # needs a technical visit


@pytest.mark.parametrize("phone", ["600111002", "600111009"])
async def test_live_weak_signal(live, phone):
    await live.say("c", f"Hola, mi número es {phone}")
    r = await live.say("c", "Internet me va muy lento todo el día")
    assert_checked_network(live)
    assert r.status == "handoff", live.transcript          # weak signal -> technician


async def test_live_intermittent(live):
    await live.say("c", "Hola, soy el +34 600 111 004")
    r = await live.say("c", "Se me corta la conexión varias veces al día")
    assert_checked_network(live)
    assert "get_signal_history" in live.ok_tools(), live.transcript
    assert r.status == "handoff", live.transcript


async def test_live_all_good_then_escalate(live):
    await live.say("c", "Hola, soy el 600111001")
    r1 = await live.say("c", "Me va lento el wifi en el salón")
    assert_checked_network(live)
    assert r1.status == "active", live.transcript          # healthy line: troubleshoot first
    assert re.search(r"reinici|router|cable|wifi|dispositivo", r1.reply, re.I), live.transcript
    await live.say("c", "Ya lo he probado y sigue igual")
    r3 = await live.say("c", "También lo he probado y sigue igual de lento")
    assert r3.status == "handoff", live.transcript          # 2 failed attempts -> escalate


async def test_live_unknown_customer(live):
    r = await live.say("c", "No tengo internet, mi teléfono es 699 000 000")
    assert "find_customer" in live.ok_tools(), live.transcript
    assert "get_line_status" not in live.tools(), live.transcript   # can't diagnose a stranger
    assert r.status == "active" and "?" in r.reply, live.transcript  # asks to re-check


async def test_live_billing_escalates(live):
    await live.say("c", "Soy el 600111008")
    r = await live.say("c", "Me habéis cobrado dos veces este mes, quiero que me lo devolváis")
    assert r.status == "handoff", live.transcript


async def test_live_mcp_server_down(tmp_path, mock_db, http_mock):
    bot, _ = await _live(tmp_path, http_mock.url)
    lv = Live(bot, mock_db)
    http_mock.stop()
    r = await lv.say("c", "Hola, soy el 600111005 y no tengo internet")
    assert lv.tools() and not lv.ok_tools(), lv.transcript          # it tried; everything failed
    assert "INC-" not in r.reply and not re.search(r"\b\d{1,2}:\d{2}\b", r.reply), lv.transcript
    assert r.reply and r.status == "active", lv.transcript          # graceful answer, no invented ETA


async def test_live_prompt_cache_hits(live):
    await live.say("c", "Hola, soy el 600111008")
    await live.say("c", "Va todo bien, solo quería comprobar mi línea")
    cached = [r.usage.get("cache_read_input_tokens", 0) for r in live.results]
    assert cached[-1] > 0, f"no cache reads: {cached}"
