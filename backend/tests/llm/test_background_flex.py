"""Background work asks for the provider's Flex lane; nothing a reader waits on ever does.

Every request goes through a stand-in HTTP transport, so the bodies checked are the bytes the SDK
would send to OpenAI. No real provider is called.
"""
import json

import httpx
import pytest

from app.cost.limits import budgeted_completion, reserved_usd_of, usd_of
from app.llm.client import FLEX_TIMEOUT_SECONDS, LLMClient, ProviderUnavailable, Reusable
from app.stream_conversation import SECOND_LOOK_SYSTEM, STREAM_SYSTEM, SentenceVerdict


def _body(model, content, tier):
    return {"id": "t", "object": "chat.completion", "created": 0, "model": model, "service_tier": tier,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}}


def _endpoint(content='{"supported":true}', *, flex=None, served=None):
    """A stand-in OpenAI endpoint keeping every request body and its read timeout. ``flex`` is
    how it answers a Flex request (None: served on Flex), ``served`` the tier it reports."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((body, request.extensions["timeout"]["read"]))
        if body.get("service_tier") == "flex" and flex == "busy":
            return httpx.Response(429, json={"error": {"message": "Resource Unavailable",
                                                       "type": "resource_unavailable", "code": None}})
        if body.get("service_tier") == "flex" and flex == "gone":
            return httpx.Response(400, json={"error": {
                "message": "Invalid value for service_tier: 'flex'.", "type": "invalid_request_error",
                "param": "service_tier", "code": None}})
        if body.get("stream"):
            chunks = [{"id": "s", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                       "choices": [{"index": 0, "delta": {"content": "Fine."}, "finish_reason": None}]},
                      {"id": "s", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                       "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}}]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")
        return httpx.Response(200, json=_body(body["model"], content, served or body.get("service_tier")))
    return handler, seen


def _client(handler, base_url=None):
    return LLMClient(provider="openai-compatible", api_key="synthetic", env={}, base_url=base_url,
                     max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _background(handler):
    client = _client(handler)
    assert client.use_flex_for_background() is True
    return client


def test_a_background_call_goes_on_flex_with_the_longer_timeout_and_everything_else_unchanged():
    handler, seen = _endpoint()
    plain = _client(handler)
    plain.complete(SECOND_LOOK_SYSTEM, "look", tier="large", schema=SentenceVerdict)
    value, usage = _background(handler).complete(SECOND_LOOK_SYSTEM, "look", tier="large",
                                                 schema=SentenceVerdict)
    assert value == {"supported": True}
    (standard, standard_timeout), (flex, flex_timeout) = seen
    assert standard["service_tier"] == "default" and flex["service_tier"] == "flex"
    assert standard_timeout == 120.0 and flex_timeout == FLEX_TIMEOUT_SECONDS
    # Same model, effort, cap, messages, schema and storage: only the lane differs.
    assert {k: v for k, v in flex.items() if k != "service_tier"} == {
        k: v for k, v in standard.items() if k != "service_tier"}
    assert usage == {"in": 1000, "out": 100, "service_tier": "flex"}


def test_an_unstructured_background_call_goes_on_flex_too():
    handler, seen = _endpoint(content="A note.")
    value, usage = _background(handler).complete("Write notes.", "Chapter.", tier="large")
    assert value == "A note." and usage["service_tier"] == "flex"
    assert seen[0][0]["service_tier"] == "flex"


def test_flex_without_room_sends_the_same_call_standard_at_once():
    handler, seen = _endpoint(flex="busy")
    client = _background(handler)
    look = Reusable('{"passages":["p"],"sentence":"s"}', 14)
    value, usage = client.complete(SECOND_LOOK_SYSTEM, look, tier="large", schema=SentenceVerdict)
    assert value == {"supported": True}
    assert usage == {"in": 1000, "out": 100}          # served standard: priced standard
    (busy, busy_timeout), (retried, retried_timeout) = seen
    assert busy["service_tier"] == "flex" and retried["service_tier"] == "default"
    assert busy_timeout == FLEX_TIMEOUT_SECONDS and retried_timeout == 120.0
    assert {k: v for k, v in busy.items() if k != "service_tier"} == {
        k: v for k, v in retried.items() if k != "service_tier"}
    assert client.provider_status()["completion"]["status"] == "ready"
    # A busy lane is no refusal: the next background call asks for Flex again.
    client.complete("Write notes.", "Chapter.", tier="large")
    assert seen[2][0]["service_tier"] == "flex"


def test_a_provider_that_no_longer_offers_flex_gets_standard_calls_from_then_on():
    handler, seen = _endpoint(content="A note.", flex="gone")
    client = _background(handler)
    assert client.complete("Write notes.", "Chapter.", tier="large")[0] == "A note."
    client.complete("Write notes.", "Chapter.", tier="large")
    assert [body["service_tier"] for body, _ in seen] == ["flex", "default", "default"]


def test_any_other_refusal_of_a_flex_call_is_not_retried():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "Invalid value for reasoning_effort.",
                                                   "type": "invalid_request_error",
                                                   "param": "reasoning_effort", "code": None}})
    with pytest.raises(ProviderUnavailable):
        _background(handler).complete("Write notes.", "Chapter.", tier="large")


@pytest.mark.parametrize("schema", [None, SentenceVerdict])
def test_a_flex_answer_to_a_call_that_did_not_ask_for_flex_is_still_refused(schema):
    handler, _ = _endpoint(content='{"supported":true}', served="flex")
    with pytest.raises(ProviderUnavailable) as error:
        _client(handler).complete(SECOND_LOOK_SYSTEM, "look", tier="large", schema=schema)
    assert error.value.kind == "unsupported_service_tier"


def test_a_flex_call_answered_on_another_paid_lane_is_refused():
    handler, _ = _endpoint(served="priority")
    with pytest.raises(ProviderUnavailable) as error:
        _background(handler).complete(SECOND_LOOK_SYSTEM, "look", tier="large", schema=SentenceVerdict)
    assert error.value.kind == "unsupported_service_tier"


def test_a_spoken_reply_never_goes_on_flex_even_from_a_background_client():
    handler, seen = _endpoint()
    client = _background(handler)
    client.on_text_delta = lambda text: None
    assert client.complete(Reusable(STREAM_SYSTEM), "Reply now.", tier="large")[0] == "Fine."
    assert seen[0][0]["service_tier"] == "default" and seen[0][1] == 120.0


def test_only_the_native_openai_client_takes_flex():
    handler, seen = _endpoint(content="Fine.")
    compatible = _client(handler, base_url="https://compatible.example/v1")
    assert compatible.use_flex_for_background() is False
    compatible.complete("Write notes.", "Chapter.", tier="large")
    assert "service_tier" not in seen[0][0]
    assert LLMClient(env={"LLM_TRANSPORT": "codex-local"}).use_flex_for_background() is False
    assert LLMClient(env={"ANTHROPIC_API_KEY": "synthetic"}).use_flex_for_background() is False
    assert LLMClient(provider="stub", allow_stub=True, env={}).use_flex_for_background() is False


# -- what the ledger records -------------------------------------------------------------------

def test_flex_is_priced_at_the_flex_rates_and_standard_is_unchanged():
    usage = {"in": 10_000, "out": 2_000, "cache_read": 6_000, "cache_write": 1_000}
    standard = usd_of("gpt-6.1-terra", usage)
    assert standard == pytest.approx(3_000 * 1.00e-6 + 6_000 * 0.05e-6 + 1_000 * 1.25e-6 + 2_000 * 5.00e-6)
    flex = usd_of("gpt-6.1-terra", {**usage, "service_tier": "flex"})
    assert flex == pytest.approx(3_000 * 0.50e-6 + 6_000 * 0.025e-6 + 1_000 * 0.625e-6 + 2_000 * 2.50e-6)
    assert flex == pytest.approx(standard / 2)
    for model in ("gpt-6-luna", "gpt-6.1-sol"):
        assert usd_of(model, {**usage, "service_tier": "flex"}) == pytest.approx(usd_of(model, usage) / 2)
    # Long context keeps its premiums on Flex as on Standard (pricing page: $1.00 in, $3.75 out).
    long = {"in": 300_000, "out": 1_000, "service_tier": "flex"}
    assert usd_of("gpt-6.1-terra", long) == pytest.approx(300_000 * 1.00e-6 + 1_000 * 3.75e-6)
    # Another tier name, or a subscription identity, is never priced as Flex.
    assert usd_of("gpt-6.1-terra", {**usage, "service_tier": "default"}) == standard
    assert usd_of("chatgpt-plan:gpt-6.1-terra", {**usage, "service_tier": "flex"}) == 0.0


class _Ledger:
    def __init__(self):
        self.reserved, self.actual, self.settled = [], [], []

    def reserve_cost(self, book_id, **call):
        self.reserved.append(call)
        return "r1"

    def note_reservation_actual(self, book_id, reservation_id, **actual):
        self.actual.append(actual)

    def settle_cost(self, book_id, reservation_id, **kwargs):
        self.settled.append(reservation_id)


class _Settings:
    cost_max_input_tokens_per_call = 200_000
    cost_max_output_tokens_per_call = 4096
    cost_max_input_tokens_per_book = 10_000_000
    cost_max_output_tokens_per_book = 10_000_000
    cost_max_usd_per_book = 100.0


@pytest.mark.parametrize("flex", [None, "busy"])
def test_the_ledger_reserves_standard_and_records_what_the_lane_served_costs(flex):
    handler, _ = _endpoint(content="A note.", flex=flex)
    ledger = _Ledger()
    result = budgeted_completion(ledger, _Settings(), _background(handler), "book", phase="notes-write",
                                 system="Write notes.", user="Chapter. " * 200, tier="large")
    (reserved,) = ledger.reserved
    assert reserved["usd"] == reserved_usd_of("gpt-6.1-terra", {"in": reserved["input_tokens"],
                                                               "out": reserved["output_tokens"]})
    (actual,) = ledger.actual
    standard = 1000 * 1.00e-6 + 100 * 5.00e-6
    expected = standard / 2 if flex is None else standard
    assert actual["usd"] == pytest.approx(expected) and result.usd == pytest.approx(expected)
    assert result.usage == {"in": 1000, "out": 100}
    assert ledger.settled == ["r1"]
