"""Langfuse tracing: spans are captured in memory instead of being sent anywhere."""
import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.tracing import Tracer, make_client
from tests.helpers import resp, text, tool

A = "langfuse.observation."


@pytest.fixture
def traced(env, request):
    exporter = InMemorySpanExporter()
    # The SDK keeps one set of resources per public key: use a fresh key per test.
    client = make_client(public_key=f"pk-{request.node.name}", secret_key="sk-test",
                         span_exporter=exporter)
    tracer = Tracer(client)
    plain_make = env.make

    def make(script, **cfg):
        bot, fake = plain_make(script, **cfg)
        bot.tracer = tracer
        return bot, fake

    def spans():
        client.flush()
        return exporter.get_finished_spans()

    env.make, env.spans = make, spans
    yield env
    client.shutdown()


def by_name(spans, name):
    return [s for s in spans if s.name == name]


async def test_trace_shape_session_customer_and_masking(traced):
    bot, _ = traced.make([
        resp(tool("find_customer", phone_or_account="600 111 005")),
        resp(tool("get_line_status", line_id="LN-1005"),
             tool("get_node_status", node_id="NODO-NORTE-01")),
        resp(text("Hola Ana, hay una avería en tu zona.")),
    ])
    await bot.handle_message("conv-1", "Hola, soy Ana López, mi teléfono es 600 111 005")
    spans = traced.spans()

    (root,) = by_name(spans, "chat-turn")
    llms = by_name(spans, "llm")
    tools = [s for s in spans if s.attributes[A + "type"] == "tool"]
    assert len(llms) == 3 and all(s.attributes[A + "type"] == "generation" for s in llms)
    assert sorted(s.name for s in tools) == ["find_customer", "get_line_status", "get_node_status"]
    # One trace, everything under the root.
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert all(s.parent.span_id == root.context.span_id for s in spans if s is not root)

    # Grouped by conversation; customer attached from the moment it is known.
    assert all(s.attributes["session.id"] == "conv-1" for s in spans)
    assert root.attributes["user.id"] == "CUS-005"
    assert "customer:CUS-005" in root.attributes["langfuse.trace.tags"]
    assert sum(s.attributes.get("user.id") == "CUS-005" for s in llms) == 2   # after the lookup

    # Tool span: input, output, real duration.
    (find,) = by_name(spans, "find_customer")
    assert json.loads(find.attributes[A + "input"]) == {"phone_or_account": "[PHONE]"}
    out = json.loads(find.attributes[A + "output"])
    assert out["customer"]["name"] == "[NAME]" and out["customer"]["phone"] == "[PHONE]"
    assert out["customer"]["customer_id"] == "CUS-005"
    assert find.end_time > find.start_time
    assert root.start_time <= find.start_time and find.end_time <= root.end_time

    # Generation: full prompt (system, tools, messages) and the response.
    prompt = json.loads(llms[0].attributes[A + "input"])
    assert set(prompt) == {"system", "tools", "messages"}
    assert prompt["messages"][0]["content"] == "Hola, soy [NAME], mi teléfono es [PHONE]"
    assert json.loads(llms[-1].attributes[A + "output"])["content"][0]["text"] == \
        "Hola [NAME], hay una avería en tu zona."
    assert root.attributes[A + "output"] == "Hola [NAME], hay una avería en tu zona."

    # No raw PII anywhere in what gets exported - including the first LLM call,
    # which ran before the customer was identified.
    blob = json.dumps([dict(s.attributes) for s in spans], ensure_ascii=False, default=str)
    for secret in ("600111005", "600 111 005", "Ana", "López", "Lopez"):
        assert secret not in blob, secret


async def test_usage_and_cost_on_generation(traced):
    r = resp(text("ok"))
    r.model = "claude-opus-5-5"
    r.usage.input_tokens, r.usage.output_tokens = 1000, 100
    r.usage.cache_read_input_tokens, r.usage.cache_creation_input_tokens = 20_000, 4000
    bot, _ = traced.make([r])
    await bot.handle_message("conv-2", "hola")
    (llm,) = by_name(traced.spans(), "llm")
    assert llm.attributes[A + "model.name"] == "claude-opus-5-5"
    assert json.loads(llm.attributes[A + "usage_details"]) == {
        "input": 1000, "output": 100, "cache_read_input_tokens": 20_000,
        "cache_creation_input_tokens": 4000, "total": 25_100}
    cost = json.loads(llm.attributes[A + "cost_details"])
    assert cost["input"] == pytest.approx(0.004) and cost["output"] == pytest.approx(0.002)
    assert cost["cache_read_input_tokens"] == pytest.approx(0.004)
    assert cost["cache_creation_input_tokens"] == pytest.approx(0.02)
    assert cost["total"] == pytest.approx(0.03)


async def test_tool_error_is_marked(traced):
    bot, _ = traced.make([resp(tool("get_line_status", line_id="LN-NOPE")), resp(text("vaya"))])
    await bot.handle_message("conv-3", "hola")
    (span,) = by_name(traced.spans(), "get_line_status")
    assert span.attributes[A + "level"] == "ERROR"
    assert span.attributes[A + "status_message"]


async def test_known_customer_tagged_from_first_span(traced):
    bot, _ = traced.make([resp(tool("find_customer", phone_or_account="ACC-50001")),
                          resp(text("uno")), resp(text("dos"))])
    await bot.handle_message("conv-4", "soy ACC-50001")
    traced.spans()
    await bot.handle_message("conv-4", "sigo sin internet")
    second = [s for s in traced.spans() if s.attributes[A + "type"] != "tool"][-2:]
    assert {s.name for s in second} == {"llm", "chat-turn"}
    assert all(s.attributes["user.id"] == "CUS-001" for s in second)
    assert len({s.context.trace_id for s in traced.spans()}) == 2      # one trace per message


async def test_disabled_tracer_changes_nothing(env):
    bot, _ = env.make([resp(text("hola"))])
    assert not bot.tracer.enabled
    assert (await bot.handle_message("conv-5", "hola")).reply == "hola"
