"""Prompt caching changes only how a native OpenAI call is billed, never what the model reads.

Every request here goes through a stand-in HTTP transport, so the bodies checked are the bytes the
SDK would send to OpenAI. No real provider is called.
"""
import json

import httpx
import pytest

from app.llm import codex_local
from app.llm.client import LLMClient, ProviderUnavailable, Reusable
from app.stream_conversation import (SECOND_LOOK_SYSTEM, STREAM_SYSTEM, STREAM_SYSTEM_REMEMBERING,
                                     SentenceVerdict, second_look_prompt)

EXPLICIT = {"mode": "explicit"}
PASSAGES = ["Elizabeth walked three miles to Netherfield.", "Jane had caught a cold in the rain.",
            "Reading notes on chapter 7: Jane falls ill at Netherfield."]
QUESTION = json.dumps({"latest": "why did she walk?", "earlier": [{"role": "reader", "text": "hi"}]})
NAMES = [("Elizabeth", "Lizzy", "Eliza")]
REPLY_PROMPT = ("READER QUESTION:\nwhy did she walk?\n\nSOURCE PASSAGES:\n[1] Elizabeth walked "
                "three miles.\n\nWHERE THE READER IS:\nChapter 7\n\nReply to the reader now.")


def before_this_change(sentence, earlier, passages, *, question, identities=()):
    """The second look's message exactly as it was built before it was reordered."""
    return json.dumps({"reader_message": question, "earlier_reply_sentences": list(earlier),
                       "same_person_names": [list(group) for group in identities],
                       "sentence": sentence, "passages": passages}, ensure_ascii=False)


def _chat_body(model, content, usage=None):
    return {"id": "test", "object": "chat.completion", "created": 0, "model": model,
            "service_tier": "default",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": usage or {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


def _stream_body(model, words, usage):
    chunks = [{"id": "s", "object": "chat.completion.chunk", "created": 0, "model": model,
               "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}]}
              for word in words]
    chunks.append({"id": "s", "object": "chat.completion.chunk", "created": 0, "model": model,
                   "choices": [], "usage": usage})
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _client(handler, base_url=None):
    return LLMClient(provider="openai-compatible", api_key="synthetic", env={}, base_url=base_url,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _recording(content='{"supported":true}', usage=None, refuse=None):
    """A stand-in OpenAI endpoint that keeps every request body. ``refuse`` returns a 400 for
    requests that carry ``prompt_cache_options``, naming that parameter."""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if refuse is not None and "prompt_cache_options" in body:
            return httpx.Response(400, json={"error": {
                "message": refuse["message"], "type": "invalid_request_error",
                "param": refuse["param"], "code": None}})
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=_stream_body(body["model"], ["It ", "rained."], usage or {
                                      "prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}))
        return httpx.Response(200, json=_chat_body(body["model"], content, usage))
    return handler, bodies


def _streamed_reply(client, system, prompt):
    words = []
    client.on_text_delta = words.append
    try:
        value, usage = client.complete(system, prompt, tier="large")
    finally:
        client.on_text_delta = None
    return value, usage, words


# -- the reply ---------------------------------------------------------------------------------

def test_the_reply_reads_the_same_bytes_with_its_fixed_instructions_marked_for_reuse():
    handler, bodies = _recording(usage={"prompt_tokens": 2400, "completion_tokens": 2, "total_tokens": 2402,
                                        "prompt_tokens_details": {"cached_tokens": 1792,
                                                                  "cache_write_tokens": 0}})
    client = _client(handler)
    value, usage, words = _streamed_reply(client, Reusable(STREAM_SYSTEM), REPLY_PROMPT)
    assert value == "It rained." and words == ["It ", "rained."]
    (body,) = bodies
    assert body["prompt_cache_options"] == EXPLICIT
    system, user = body["messages"]
    # The writer's instructions are one text part, byte for byte, ending at the breakpoint.
    assert system == {"role": "system", "content": [
        {"type": "text", "text": STREAM_SYSTEM, "prompt_cache_breakpoint": EXPLICIT}]}
    # The reader's message is sent exactly as before: one plain string.
    assert user == {"role": "user", "content": REPLY_PROMPT}
    # Everything else in the request is as it was: model, effort, cap, tier, storage, streaming.
    today = {key: value for key, value in body.items() if key not in {"messages", "prompt_cache_options"}}
    assert today == {"model": "gpt-6.1-terra", "stream": True, "stream_options": {"include_usage": True},
                     "max_completion_tokens": 4096, "reasoning_effort": "medium",
                     "service_tier": "default", "store": False}
    # The split of a cached reply still reaches the ledger.
    assert usage == {"in": 2400, "out": 2, "cache_read": 1792}


def test_the_remembering_reply_and_every_other_call_carry_no_breakpoint_only_explicit_mode():
    handler, bodies = _recording(content="Fine.")
    client = _client(handler)
    client.complete(STREAM_SYSTEM_REMEMBERING, REPLY_PROMPT, tier="large")
    client.complete("Write chapter notes.", "Chapter text.", tier="cheap")
    for body in bodies:
        assert body["prompt_cache_options"] == EXPLICIT
        assert all(isinstance(message["content"], str) for message in body["messages"])
    assert bodies[0]["messages"][0]["content"] == STREAM_SYSTEM_REMEMBERING


# -- the second look ---------------------------------------------------------------------------

def test_the_second_look_holds_the_same_fields_and_values_only_in_a_new_order():
    for sentence, earlier in [("She walked three miles.", []),
                              ("Jane was ill, and Lizzy went to her.", ["She walked three miles."])]:
        new = second_look_prompt(sentence, earlier, PASSAGES, question=QUESTION, identities=NAMES)
        old = before_this_change(sentence, earlier, PASSAGES, question=QUESTION, identities=NAMES)
        assert json.loads(new) == json.loads(old)
        assert sorted(json.loads(new, object_pairs_hook=list)) == sorted(json.loads(old, object_pairs_hook=list))
        assert [key for key, _ in json.loads(new, object_pairs_hook=list)] == [
            "reader_message", "same_person_names", "passages", "earlier_reply_sentences", "sentence"]
        assert len(new) == len(old)


def test_every_second_look_of_one_reply_shares_its_marked_part_and_the_model_reads_the_same_text():
    handler, bodies = _recording(usage={"prompt_tokens": 4300, "completion_tokens": 5, "total_tokens": 4305,
                                        "prompt_tokens_details": {"cached_tokens": 3900,
                                                                  "cache_write_tokens": 0}})
    client = _client(handler)
    looks = [second_look_prompt(sentence, earlier, PASSAGES, question=QUESTION, identities=NAMES)
             for sentence, earlier in [("She walked three miles.", []),
                                       ("Lizzy went to Jane.", ["She walked three miles."])]]
    for look in looks:
        value, usage = client.complete(SECOND_LOOK_SYSTEM, look, tier="large", schema=SentenceVerdict)
        assert value == {"supported": True}
        assert usage == {"in": 4300, "out": 5, "cache_read": 3900}
    heads = []
    for look, body in zip(looks, bodies, strict=True):
        assert body["prompt_cache_options"] == EXPLICIT
        assert body["response_format"]["type"] == "json_schema"
        system, user = body["messages"]
        assert system == {"role": "system", "content": SECOND_LOOK_SYSTEM}
        head, tail = user["content"]
        assert head["prompt_cache_breakpoint"] == EXPLICIT and "prompt_cache_breakpoint" not in tail
        # The parts join to exactly the message, which is valid JSON on its own.
        assert head["text"] + tail["text"] == str(look)
        assert json.loads(head["text"] + tail["text"]) == json.loads(look)
        assert '"sentence"' not in head["text"] and '"passages"' not in tail["text"]
        heads.append(head["text"])
    assert heads[0] == heads[1]


# -- the safety net ----------------------------------------------------------------------------

@pytest.mark.parametrize("param,message", [
    ("prompt_cache_options", "Unrecognized request argument supplied: prompt_cache_options"),
    ("messages[1].content[0].prompt_cache_breakpoint", "Unknown parameter: 'prompt_cache_breakpoint'."),
])
def test_a_refused_cache_option_is_retried_once_exactly_as_before_and_then_left_off(param, message):
    handler, bodies = _recording(refuse={"param": param, "message": message})
    client = _client(handler)
    look = second_look_prompt("She walked three miles.", [], PASSAGES, question=QUESTION, identities=NAMES)
    value, _ = client.complete(SECOND_LOOK_SYSTEM, look, tier="large", schema=SentenceVerdict)
    assert value == {"supported": True}
    refused, retried = bodies
    assert "prompt_cache_options" in refused
    assert "prompt_cache_options" not in retried
    assert retried["messages"] == [{"role": "system", "content": SECOND_LOOK_SYSTEM},
                                   {"role": "user", "content": str(look)}]
    assert {k: v for k, v in refused.items() if k not in {"messages", "prompt_cache_options"}} == {
        k: v for k, v in retried.items() if k != "messages"}
    # A spoken reply after that goes straight out without the options: one request, not two.
    value, _, words = _streamed_reply(client, Reusable(STREAM_SYSTEM), REPLY_PROMPT)
    assert value == "It rained." and words == ["It ", "rained."]
    assert len(bodies) == 3 and "prompt_cache_options" not in bodies[2]
    assert bodies[2]["messages"] == [{"role": "system", "content": STREAM_SYSTEM},
                                     {"role": "user", "content": REPLY_PROMPT}]
    assert client.provider_status()["completion"]["status"] == "ready"


def test_a_spoken_reply_refused_for_its_cache_option_is_still_written():
    handler, bodies = _recording(refuse={"param": "prompt_cache_options", "message": "Unsupported."})
    client = _client(handler)
    value, _, words = _streamed_reply(client, Reusable(STREAM_SYSTEM), REPLY_PROMPT)
    assert value == "It rained." and words == ["It ", "rained."]
    assert [("prompt_cache_options" in body) for body in bodies] == [True, False]


def test_any_other_refusal_is_not_retried():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "Invalid value for reasoning_effort.",
                                                   "type": "invalid_request_error",
                                                   "param": "reasoning_effort", "code": None}})
    client = _client(handler)
    with pytest.raises(ProviderUnavailable):
        client.complete(Reusable(STREAM_SYSTEM), REPLY_PROMPT, tier="large")
    assert len(bodies) == 1


# -- other providers are left as they were -----------------------------------------------------

def test_another_openai_compatible_provider_gets_plain_strings_and_no_cache_options():
    handler, bodies = _recording(content="Fine.")
    client = _client(handler, base_url="https://compatible.example/v1")
    client.complete(Reusable(STREAM_SYSTEM), REPLY_PROMPT, tier="large")
    (body,) = bodies
    assert "prompt_cache_options" not in body
    assert body["messages"] == [{"role": "system", "content": STREAM_SYSTEM},
                                {"role": "user", "content": REPLY_PROMPT}]


def test_the_chatgpt_subscription_transport_receives_the_same_text(monkeypatch):
    seen = []

    def complete(system, user, **kwargs):
        seen.append((system, user, kwargs))
        return {"supported": True}, {"in": 1, "out": 1}
    monkeypatch.setattr(codex_local, "complete", complete)
    client = LLMClient(env={"LLM_TRANSPORT": "codex-local"})
    look = second_look_prompt("She walked three miles.", [], PASSAGES, question=QUESTION, identities=NAMES)
    client.complete(SECOND_LOOK_SYSTEM, look, tier="large", schema=SentenceVerdict)
    ((system, user, kwargs),) = seen
    assert system == SECOND_LOOK_SYSTEM and user == str(look)
    assert not any("cache" in key for key in kwargs)
