import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import tool_names as T
from app.bot import BotConfig, ChatBot
from app.config import Settings, get_settings
from app.mcp_client import MCPConnection, MCPToolbox
from app.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


def build_connections(s: Settings) -> list[MCPConnection]:
    conns = [MCPConnection(s.mcp_signal_server, s.mcp_signal_token)]
    if s.mcp_customer_server:
        conns.append(MCPConnection(s.mcp_customer_server, s.mcp_signal_token))
    return conns


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    db = Path(s.app_db_path)
    storage = Storage(db if db.is_absolute() else ROOT / db)
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key or None)
    async with MCPToolbox(build_connections(s), s.mcp_call_timeout) as toolbox:
        app.state.toolbox = toolbox
        app.state.storage = storage
        app.state.bot = ChatBot(client, toolbox, storage, BotConfig(
            model=s.bot_model, summary_model=s.bot_summary_model, effort=s.bot_effort,
            max_tool_calls=s.bot_max_tool_calls, history_window=s.bot_history_window,
            timezone=s.bot_timezone, use_fallbacks=s.bot_fallbacks,
        ))
        yield


app = FastAPI(title="ISP Support Bot (trial)", lifespan=lifespan)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


class ChatOut(BaseModel):
    conversation_id: str
    reply: str
    status: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]


@app.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn, request: Request) -> ChatOut:
    conv_id = body.conversation_id or f"web-{uuid.uuid4().hex[:12]}"
    result = await request.app.state.bot.handle_message(conv_id, body.message.strip())
    out = ChatOut(**result.__dict__)
    if not get_settings().chat_debug:      # don't expose tool data to real customers
        out.tool_calls, out.usage = [], {}
    return out


@app.get("/api/conversations/{conversation_id}")
async def conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    storage: Storage = request.app.state.storage
    conv = storage.get_or_create(conversation_id)
    messages = storage.transcript(conversation_id)
    if not get_settings().chat_debug:
        for m in messages:
            m["tool_calls"], m["usage"], m["tool_notes"] = [], {}, ""
    return {"conversation_id": conv.conversation_id, "status": conv.status,
            "messages": messages}


# --- Pages ------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def chat_page() -> FileResponse:
    return FileResponse(STATIC / "chat.html")


@app.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(STATIC / "admin.html")


# --- Admin data (trial: no auth; keep the app on a private network) ---------
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


@app.get("/admin/api/overview")
async def admin_overview(request: Request) -> dict[str, Any]:
    storage: Storage = request.app.state.storage
    convs = storage.conversations_overview()
    totals = {k: sum(c[k] for c in convs) for k in USAGE_KEYS}
    totals["conversations"] = len(convs)
    totals["handoffs"] = sum(c["status"] == "handoff" for c in convs)
    return {
        "model": get_settings().bot_model,
        "totals": totals,
        "conversations": convs,
        "tickets": storage.successful_tool_outputs(T.CREATE_TICKET),
        "handoffs": storage.successful_tool_outputs(T.HANDOFF),
    }


@app.get("/admin/api/conversations/{conversation_id}")
async def admin_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    storage: Storage = request.app.state.storage
    conv = storage.get_or_create(conversation_id)
    return {**conv.__dict__, "messages": storage.transcript(conversation_id)}


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    s = get_settings()
    return {"status": "ok", "mcp_signal_server": s.mcp_signal_server,
            "tools": request.app.state.toolbox.tool_names, "model": s.bot_model}


# --- Debug endpoints: call MCP tools directly (trial only) -----------------
@app.get("/debug/tools")
async def debug_tools(request: Request) -> list[dict[str, Any]]:
    return request.app.state.toolbox.anthropic_tools()


@app.post("/debug/tools/{name}")
async def debug_call(name: str, request: Request,
                     arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    res = await request.app.state.toolbox.call(name, arguments or {})
    if res.is_error:
        raise HTTPException(502, res.text)
    return res.structured or {"text": res.text}
