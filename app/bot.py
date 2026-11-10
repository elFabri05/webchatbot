"""Chatbot core: Claude Messages API + MCP tools, manual tool-use loop.

Request layout (render order tools -> system -> messages):
  tools    : MCP tools, sorted, built once          [cache breakpoint 1]
  system   : SYSTEM_PROMPT, frozen at startup       [cache breakpoint 2]
  messages : summary-window history (text only)
             + role:"system" context message (customer, time, summary, recent tool data)
             + this turn's assistant/tool_result blocks, appended verbatim
                                                    [breakpoint 3: last tool_result, moves]

History across turns is persisted as plain text, so thinking blocks are never
replayed into a later turn; trimming the window therefore can't trip the
preserved-thinking history check. Within a turn, `response.content` is appended
unchanged (thinking + tool_use), as the API requires.
"""
import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from app import tool_names as T
from app.mcp_client import MCPToolbox, ToolResult
from app.prompts import CONTEXT_TEMPLATE, SUMMARY_SYSTEM, SYSTEM_PROMPT
from app.storage import Conversation, StoredMessage, Storage
from app.tracing import Tracer, Turn

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_API_ROUNDS = 8          # hard stop for the loop, independent of the tool budget
TOOL_NOTE_CHARS = 400       # per tool call, in stored notes

MSG_HANDOFF_ACTIVE = ("Tu caso ya está con una persona de nuestro equipo, que te responderá "
                      "por aquí en cuanto pueda. ¡Gracias por tu paciencia!")
MSG_ERROR = ("Lo siento, ahora mismo tengo un problema técnico para atenderte. "
             "¿Puedes intentarlo de nuevo en unos minutos?")
MSG_REFUSAL = ("Lo siento, no puedo ayudarte con eso por aquí. Si es sobre tu servicio de "
               "internet, cuéntame qué problema tienes y lo revisamos.")


@dataclass
class BotConfig:
    model: str = "claude-opus-5-5"
    summary_model: str = ""             # empty = same as model
    effort: str = "medium"
    max_tokens: int = 16000
    max_tool_calls: int = 5
    history_window: int = 10
    timezone: str = "Europe/Madrid"
    use_fallbacks: bool = True


@dataclass
class TurnResult:
    conversation_id: str
    reply: str
    status: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class ChatBot:
    def __init__(self, client: anthropic.AsyncAnthropic, toolbox: MCPToolbox,
                 storage: Storage, config: BotConfig, tracer: Tracer | None = None):
        self.client = client
        self.tracer = tracer or Tracer(None)     # no-op unless Langfuse is enabled
        self.toolbox = toolbox
        self.storage = storage
        self.cfg = config
        self.tz = ZoneInfo(config.timezone)
        # Frozen for the process lifetime -> stable cached prefix.
        self.system = [{
            "type": "text",
            "text": SYSTEM_PROMPT.format(max_tool_calls=config.max_tool_calls, **T.PROMPT_NAMES),
            "cache_control": {"type": "ephemeral"},
        }]
        self.tools = toolbox.anthropic_tools()
        missing = [n for n in T.REQUIRED if n not in toolbox.tool_names]
        if missing:
            log.warning("Connected MCP servers don't expose %s; the prompt and bot expect them. "
                        "Fix app/tool_names.py or the server config.", ", ".join(missing))
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------ API
    def _request_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "output_config": {"effort": self.cfg.effort},
        }
        if self.cfg.use_fallbacks:
            kw["fallbacks"] = "default"
            kw["betas"] = [FALLBACK_BETA]
        return kw

    async def handle_message(self, conversation_id: str, text: str) -> TurnResult:
        async with self._locks[conversation_id]:
            return await self._turn(conversation_id, text)

    # ----------------------------------------------------------------- turn
    async def _turn(self, conversation_id: str, text: str) -> TurnResult:
        conv = self.storage.get_or_create(conversation_id)
        with self.tracer.turn(conversation_id, conv.customer, text) as trace:
            result = await self._traced_turn(conv, text, trace)
            trace.root.set(output=result.reply, metadata={
                **trace.root.fields["metadata"], "status": result.status,
                "tool_calls": [c["name"] for c in result.tool_calls], **result.usage})
            return result

    async def _traced_turn(self, conv: Conversation, text: str, trace: Turn) -> TurnResult:
        conversation_id = conv.conversation_id
        self.storage.add_message(conversation_id, "user", text)

        if conv.status == "handoff":
            self.storage.add_message(conversation_id, "assistant", MSG_HANDOFF_ACTIVE)
            return TurnResult(conversation_id, MSG_HANDOFF_ACTIVE, "handoff")

        usage: dict[str, int] = defaultdict(int)
        summary, window = await self._window(conversation_id, conv.summary,
                                             conv.summarized_through, usage, trace)
        messages: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in window]
        messages.append({"role": "system",
                         "content": self._context(conversation_id, conv.customer, summary, window)})

        calls: list[dict[str, Any]] = []
        status = conv.status
        reply = ""
        tool_choice: dict[str, str] = {"type": "auto"}

        try:
            for _ in range(MAX_API_ROUNDS):
                request = dict(self._request_kwargs(), system=self.system, tools=self.tools,
                               tool_choice=tool_choice, messages=messages)
                with trace.generation("llm", request) as gen:
                    resp = await self.client.beta.messages.create(**request)
                    gen.response(resp)
                _add_usage(usage, resp.usage)

                if resp.stop_reason == "refusal":
                    log.warning("Refusal in %s: %s", conversation_id, resp.stop_details)
                    reply = MSG_REFUSAL
                    break

                messages.append({"role": "assistant", "content": resp.content})
                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not tool_uses:
                    reply = "".join(b.text for b in resp.content if b.type == "text").strip()
                    if resp.stop_reason == "max_tokens":
                        log.warning("max_tokens hit in %s", conversation_id)
                    break

                results, status = await self._run_tools(conversation_id, tool_uses, calls,
                                                        status, trace)
                _strip_cache_control(messages)          # keep <= 4 breakpoints
                results[-1]["cache_control"] = {"type": "ephemeral"}
                messages.append({"role": "user", "content": results})

                if len(calls) >= self.cfg.max_tool_calls:
                    tool_choice = {"type": "none"}      # budget spent: answer with what we have
        except anthropic.APIError as e:
            log.exception("Claude API error in %s", conversation_id)
            trace.root.fail(e)
            reply = MSG_ERROR

        if not reply:
            reply = MSG_ERROR
        notes = _tool_notes(calls)
        self.storage.add_message(conversation_id, "assistant", reply, notes, usage)
        return TurnResult(conversation_id, reply, status, calls, dict(usage))

    async def _run_tools(self, conversation_id: str, tool_uses: list[Any],
                         calls: list[dict[str, Any]], status: str, trace: Turn
                         ) -> tuple[list[dict[str, Any]], str]:
        budget = self.cfg.max_tool_calls - len(calls)
        allowed, denied = tool_uses[:max(budget, 0)], tool_uses[max(budget, 0):]

        def args_for(block: Any) -> dict[str, Any]:
            args = dict(block.input or {})
            if block.name == T.HANDOFF:
                args["conversation_id"] = conversation_id   # never trust a model-supplied id
            return args

        async def call(name: str, args: dict[str, Any]) -> ToolResult:
            with trace.tool(name, args) as obs:
                res = await self.toolbox.call(name, args)
                obs.set(output=res.text)
                if res.is_error:
                    obs.fail(res.text)
                return res

        arg_list = [args_for(b) for b in allowed]
        outcomes = await asyncio.gather(*(call(b.name, a) for b, a in zip(allowed, arg_list)))
        results: list[dict[str, Any]] = []
        for block, args, res in zip(allowed, arg_list, outcomes):   # request order
            self.storage.log_tool_call(conversation_id, block.name, args, res.text, res.is_error)
            calls.append({"name": block.name, "input": args, "is_error": res.is_error,
                          "output": res.text})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": res.text, "is_error": res.is_error})
            if res.is_error or not res.structured:
                continue
            if block.name == T.FIND_CUSTOMER and res.structured.get("found"):
                self.storage.set_customer(conversation_id, res.structured["customer"])
                trace.set_customer(res.structured["customer"])
            elif block.name == T.HANDOFF:
                self.storage.set_status(conversation_id, "handoff")
                status = "handoff"
        for block in denied:
            msg = (f"Not executed: limit of {self.cfg.max_tool_calls} tool calls per "
                   "customer message reached. Answer with the data you already have.")
            with trace.tool(block.name, dict(block.input or {})) as obs:
                obs.set(output=msg)
                obs.fail("Not executed: tool budget spent")
            results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                            "content": msg})
        return results, status

    # -------------------------------------------------------- history/summary
    async def _window(self, conversation_id: str, summary: str, through: int,
                      usage: dict[str, int], trace: Turn
                      ) -> tuple[str, list[StoredMessage]]:
        """Last `history_window` messages; anything older is folded into the summary."""
        msgs = self.storage.unsummarized(conversation_id, through)
        cut = max(len(msgs) - self.cfg.history_window, 0)
        while cut < len(msgs) and msgs[cut].role != "user":   # window must start with user
            cut += 1
        if cut == 0:
            return summary, msgs
        overflow, window = msgs[:cut], msgs[cut:]
        try:
            summary = await self._summarize(summary, overflow, usage, trace)
            self.storage.set_summary(conversation_id, summary, overflow[-1].message_id)
            return summary, window
        except Exception:
            # Keep going with a larger window; we'll try to summarize again next turn.
            log.exception("Summary update failed for %s", conversation_id)
            return summary, msgs

    async def _summarize(self, previous: str, overflow: list[StoredMessage],
                         usage: dict[str, int], trace: Turn) -> str:
        lines = []
        for m in overflow:
            who = "Cliente" if m.role == "user" else "Asistente"
            lines.append(f"[{m.created_at}] {who}: {m.content}")
            if m.tool_notes:
                lines.append(f"  (herramientas: {m.tool_notes})")
        prompt = (f"Resumen anterior:\n{previous or '(vacío)'}\n\n"
                  "Mensajes que salen de la ventana:\n" + "\n".join(lines))
        request = dict(model=self.cfg.summary_model or self.cfg.model, max_tokens=4000,
                       output_config={"effort": "low"}, system=SUMMARY_SYSTEM,
                       messages=[{"role": "user", "content": prompt}])
        with trace.generation("summary", request) as gen:
            resp = await self.client.beta.messages.create(**request)
            gen.response(resp)
        _add_usage(usage, resp.usage)
        if resp.stop_reason == "refusal":
            raise RuntimeError("summary refused")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise RuntimeError("empty summary")
        return text

    def _context(self, conversation_id: str, customer: dict[str, Any] | None, summary: str,
                 window: list[StoredMessage]) -> str:
        now = datetime.now(timezone.utc)
        notes = [f"  [{m.created_at}] {m.tool_notes}" for m in window
                 if m.role == "assistant" and m.tool_notes]
        return CONTEXT_TEMPLATE.format(
            conversation_id=conversation_id,
            local_now=now.astimezone(self.tz).strftime("%Y-%m-%d %H:%M"),
            timezone=self.cfg.timezone,
            utc_now=now.strftime("%Y-%m-%dT%H:%MZ"),
            customer=json.dumps(customer, ensure_ascii=False) if customer else "todavía no",
            tool_notes="\n".join(notes) or "  (ninguno)",
            summary=summary or "(no hay; la conversación es corta)",
        )


# ---------------------------------------------------------------- helpers
def _add_usage(acc: dict[str, int], u: Any) -> None:
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens"):
        acc[k] += getattr(u, k, None) or 0


def _strip_cache_control(messages: list[dict[str, Any]]) -> None:
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict):
                    b.pop("cache_control", None)


def _tool_notes(calls: list[dict[str, Any]]) -> str:
    parts = []
    for c in calls:
        if c["name"] == T.SIGNAL_HISTORY:   # samples are bulky; keep the summary only
            try:
                out = json.dumps(json.loads(c["output"])["summary"], ensure_ascii=False)
            except (ValueError, KeyError, TypeError):
                out = c["output"]
        else:
            out = c["output"]
        args = json.dumps(c["input"], ensure_ascii=False)
        parts.append(f"{c['name']}({args}) -> {'ERROR ' if c['is_error'] else ''}"
                     f"{out[:TOOL_NOTE_CHARS]}")
    return " | ".join(parts)
