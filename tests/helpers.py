"""Shared test helpers: a scripted fake Claude client and response builders."""
import copy
import itertools
import json
from types import SimpleNamespace as NS
from typing import Any

_ids = itertools.count()


def text(t: str) -> NS:
    return NS(type="text", text=t)


def tool(name: str, **inp: Any) -> NS:
    return NS(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=inp)


def resp(*blocks: NS, stop: str | None = None) -> NS:
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    return NS(content=list(blocks), stop_reason=stop, stop_details=None,
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0))


class FakeClaude:
    """Returns scripted responses; records every request body (deep-copied).
    Summary requests (plain-string system prompt) get an automatic reply."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        if isinstance(kw.get("system"), str):
            return resp(text("RESUMEN"))
        if not self.script:
            raise AssertionError("FakeClaude script exhausted: the bot made an unexpected call")
        return self.script.pop(0)

    @property
    def chat_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if not isinstance(r["system"], str)]


def outputs(result, name: str) -> list[dict[str, Any]]:
    """Parsed outputs of successful calls to `name` in a TurnResult."""
    return [json.loads(c["output"]) for c in result.tool_calls
            if c["name"] == name and not c["is_error"]]
