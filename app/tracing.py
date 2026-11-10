"""Langfuse tracing: one trace per customer message.

    chat-turn (span)            input = customer message, output = reply
      ├─ summary (generation)   only when the history window overflows
      ├─ llm (generation)       full prompt, response, tokens, cost
      ├─ <tool name> (tool)     input, output, duration, error
      └─ llm (generation)       ...

Traces are grouped by conversation (Langfuse session = conversation_id) and carry
the customer id as Langfuse user id plus a `customer:<id>` tag once it is known.

PII: observations are created when the work starts (so timings are real) but
their payloads are attached, masked, only when the turn ends. By then the masker
knows the customer's name even if they were identified halfway through the turn,
so the first LLM call's prompt is masked as well. Nothing unmasked is ever handed
to the Langfuse SDK. Never put PII in names, tags or ids: those are not masked.

With LANGFUSE_ENABLED=false, `Tracer(None)` does the same bookkeeping against
no-op observations and nothing is exported.
"""
import logging
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator

from app.pii import Masker
from app.pricing import cost_details, usage_details

log = logging.getLogger(__name__)

TRACE_NAME = "chat-turn"
STATUS_CHARS = 300


def build_tracer(settings: Any) -> "Tracer":
    """Tracer from app settings; a no-op tracer unless LANGFUSE_ENABLED is true."""
    if not settings.langfuse_enabled:
        log.info("Langfuse tracing disabled (LANGFUSE_ENABLED=false)")
        return Tracer(None)
    log.info("Langfuse tracing enabled -> %s", settings.langfuse_host)
    return Tracer(make_client(public_key=settings.langfuse_public_key,
                              secret_key=settings.langfuse_secret_key,
                              base_url=settings.langfuse_host))


def make_client(**kwargs: Any) -> Any:
    from langfuse import Langfuse
    from langfuse.span_filter import is_langfuse_span
    from opentelemetry.sdk.trace import TracerProvider

    # A private provider (not the global one) and a Langfuse-only filter: spans from
    # other OpenTelemetry instrumentation (the Anthropic SDK emits its own once a
    # global provider exists) must not reach Langfuse, because they'd skip masking.
    # `mask` is a second net under the masking done in Turn.
    return Langfuse(tracer_provider=TracerProvider(), should_export_span=is_langfuse_span,
                    mask=lambda *, data, **_: Masker()(data), **kwargs)


class Tracer:
    def __init__(self, client: Any | None):
        self.client = client

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @contextmanager
    def turn(self, conversation_id: str, customer: dict[str, Any] | None,
             user_text: str) -> Iterator["Turn"]:
        if self.client is None:
            yield Turn(None, None, conversation_id, customer, user_text)
            return
        from langfuse import propagate_attributes

        with ExitStack() as stack:
            root = stack.enter_context(self.client.start_as_current_observation(
                name=TRACE_NAME, as_type="span", end_on_exit=False))
            stack.enter_context(propagate_attributes(
                session_id=conversation_id, trace_name=TRACE_NAME))
            turn = Turn(root, stack, conversation_id, customer, user_text)
            try:
                yield turn
            except BaseException as e:
                turn.root.fail(e)
                raise
            finally:
                turn.close()

    def shutdown(self) -> None:
        if self.client is not None:
            self.client.shutdown()      # flushes pending spans


class Obs:
    """One observation whose payload is held back until the turn closes."""

    def __init__(self, lf: Any | None, input: Any = None):
        self._lf = lf
        self.fields: dict[str, Any] = {"input": input}
        self.end_ns: int | None = None

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)

    def fail(self, error: BaseException | str) -> None:
        msg = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self.fields.update(level="ERROR", status_message=msg[:STATUS_CHARS])

    def response(self, resp: Any) -> None:
        """Record a Messages API response on a generation."""
        usage = usage_details(resp.usage)
        model = getattr(resp, "model", None) or self.fields.get("model") or ""
        self.fields.update(
            output={"role": "assistant", "content": plain(resp.content)},
            model=model, usage_details=usage, cost_details=cost_details(model, usage),
            metadata={**self.fields.get("metadata", {}), "stop_reason": resp.stop_reason},
        )
        if resp.stop_reason == "refusal":
            self.fields.update(level="WARNING", status_message="refusal")

    def stop(self) -> None:
        if self.end_ns is None:
            self.end_ns = time.time_ns()

    def flush(self, mask: Masker) -> None:
        if self._lf is None:
            return
        f = {k: v for k, v in self.fields.items() if v is not None}
        for k in ("input", "output", "metadata", "status_message"):
            if k in f:
                f[k] = mask(f[k])
        self._lf.update(**f)
        self._lf.end(end_time=self.end_ns)


class Turn:
    def __init__(self, root: Any | None, stack: ExitStack | None, conversation_id: str,
                 customer: dict[str, Any] | None, user_text: str):
        self._root_lf = root
        self._stack = stack
        self._mask = Masker()
        self._children: list[Obs] = []
        self._customer_id: str | None = None
        self.root = Obs(root, input=user_text)
        self.root.set(metadata={"conversation_id": conversation_id})
        if customer:
            self.set_customer(customer)

    def set_customer(self, customer: dict[str, Any]) -> None:
        """Call as soon as the customer is known (from the task that owns the turn)."""
        self._mask.add_names(str(customer.get("name") or ""))
        cid = customer.get("customer_id")
        if not cid or cid == self._customer_id or self._stack is None:
            return
        from langfuse import propagate_attributes

        self._customer_id = cid
        # Sets user id + tag on the root span and on every observation created from now on.
        self._stack.enter_context(propagate_attributes(user_id=cid, tags=[f"customer:{cid}"]))
        self.root.set(metadata={**self.root.fields["metadata"], "customer_id": cid})

    @contextmanager
    def generation(self, name: str, request: dict[str, Any]) -> Iterator[Obs]:
        """Wrap one Messages API call. `request` is the kwargs sent to the API."""
        model = request.get("model")
        params = {"max_tokens": request.get("max_tokens"),
                  "effort": (request.get("output_config") or {}).get("effort"),
                  "tool_choice": (request.get("tool_choice") or {}).get("type")}
        prompt = {"system": request.get("system"), "messages": request.get("messages")}
        if request.get("tools"):
            prompt["tools"] = request["tools"]
        obs = self._child(name, "generation", plain(prompt))   # snapshot: messages mutate
        obs.set(model=model, model_parameters={k: v for k, v in params.items() if v is not None})
        with self._timed(obs):
            yield obs

    @contextmanager
    def tool(self, name: str, arguments: dict[str, Any]) -> Iterator[Obs]:
        """Wrap one MCP tool call; the observation's duration is the call's duration."""
        with self._timed(self._child(name, "tool", plain(arguments))) as obs:
            yield obs

    def close(self) -> None:
        self.root.stop()
        try:
            everything = [*self._children, self.root]
            for obs in everything:          # first pass only learns names
                for key in ("input", "output"):
                    self._mask(obs.fields.get(key))
            for obs in everything:          # children first, root last
                obs.flush(self._mask)
        except Exception:                   # tracing must never break a chat turn
            log.exception("Could not finalize Langfuse trace")

    # ------------------------------------------------------------- internals
    def _child(self, name: str, as_type: str, input: Any) -> Obs:
        lf = None
        if self._root_lf is not None:
            lf = self._root_lf.start_observation(name=name, as_type=as_type)
        obs = Obs(lf, input)
        self._children.append(obs)
        return obs

    @contextmanager
    def _timed(self, obs: Obs) -> Iterator[Obs]:
        try:
            yield obs
        except BaseException as e:
            obs.fail(e)
            raise
        finally:
            obs.stop()


def plain(obj: Any) -> Any:
    """Deep copy as JSON-like data: SDK models and namespaces become dicts."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items() if k != "signature"}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return plain(obj.model_dump(mode="json", exclude_none=True))
    if hasattr(obj, "__dict__"):
        return plain(vars(obj))
    return str(obj)
