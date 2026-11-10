"""Claude API list prices, used to attach a cost to each LLM call in Langfuse.

USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
(checked 2026-11-10). Cache writes are priced at the 5-minute rate, which is the
only TTL this app uses. Update this table when prices or models change; an unknown
model gets no cost (Langfuse then shows tokens only).
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Price:
    input: float
    cache_write: float
    cache_read: float
    output: float


PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(8.00, 10.00, 0.20, 40.00),
    "claude-opus-5-5": Price(4.00, 5.00, 0.20, 20.00),
    "claude-opus-5": Price(5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-5-5": Price(2.00, 2.50, 0.10, 10.00),
    "claude-sonnet-5": Price(2.00, 2.50, 0.20, 10.00),
    "claude-haiku-5-5": Price(0.10, 0.125, 0.01, 0.50),
    "claude-haiku-4-5": Price(1.00, 1.25, 0.10, 5.00),
}
# Haiku 5.5 is priced by prompt length: a request over 100K prompt tokens
# (input + cache reads + cache writes) pays these rates instead.
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.50, 0.625, 0.05, 2.50),
}


def _lookup(table: dict[str, Price], model: str) -> Price | None:
    # Longest prefix, so "claude-opus-5-5" doesn't resolve to "claude-opus-5".
    for name in sorted(table, key=len, reverse=True):
        if model.startswith(name):
            return table[name]
    return None


def usage_details(usage: Any) -> dict[str, int]:
    """Token counts of one response, keyed the way Langfuse expects."""
    def n(attr: str) -> int:
        return getattr(usage, attr, None) or 0

    d = {
        "input": n("input_tokens"),                       # uncached input only
        "output": n("output_tokens"),
        "cache_read_input_tokens": n("cache_read_input_tokens"),
        "cache_creation_input_tokens": n("cache_creation_input_tokens"),
    }
    d["total"] = sum(d.values())
    return d


def cost_details(model: str, usage: dict[str, int]) -> dict[str, float] | None:
    """USD cost per usage type for one response, or None for an unknown model."""
    price = _lookup(PRICES, model or "")
    if price is None:
        return None
    prompt = usage["total"] - usage["output"]
    if prompt > LONG_PROMPT_TOKENS:
        price = _lookup(LONG_PROMPT_PRICES, model) or price
    d = {
        "input": usage["input"] * price.input / 1e6,
        "output": usage["output"] * price.output / 1e6,
        "cache_read_input_tokens": usage["cache_read_input_tokens"] * price.cache_read / 1e6,
        "cache_creation_input_tokens":
            usage["cache_creation_input_tokens"] * price.cache_write / 1e6,
    }
    d["total"] = sum(d.values())
    return d
