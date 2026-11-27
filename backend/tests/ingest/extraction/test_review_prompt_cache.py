"""The story-memory review marks what its batches share for the prompt cache, and only that.

The text each review call sends is byte for byte what it was before: the marks only say where the
native OpenAI request splits it into parts with a cache breakpoint. No real provider is called.
"""
import json

import httpx

from app.ingest.extraction.validation import (
    EXTRACTION_REVIEW_SYSTEM,
    _REVIEW_FORMAT_REPAIR,
    ExtractionVerdict,
    extraction_review_prompt,
    review_extraction,
)
from app.llm.client import LLMClient
from tests.ingest._grounding import canned_grounding_verdict, review_payload

HEAD = "CURRENT CHAPTER AND EXTRACTION (untrusted JSON data):\n"
NAMES = [f"Reader{index}" for index in range(30)]
SOURCE = " ".join(f"{name} waited in the hall." for name in NAMES)
ROSTER = [{"canonical_name": "Warden", "type": "character", "aliases": ["the warden"]}]


def _extraction(summary="Thirty readers waited in the hall."):
    return {
        "chapter_summary": summary,
        "entities": [{"canonical_name": name, "type": "character", "aliases": [],
                      "matched_roster": False, "state": "Waited in the hall."} for name in NAMES],
        "relationships": [],
        "events": [{"summary": f"{name} waited in the hall.", "participants": [name]}
                   for name in NAMES[:20]],
        "themes": [],
    }


def before_this_change(text, extraction, roster, fields):
    """A review batch's message exactly as it was built before it was marked."""
    data = json.loads(extraction_review_prompt(text, extraction, roster).split(
        "(untrusted JSON data):\n", 1)[1])
    return "CURRENT CHAPTER AND EXTRACTION (untrusted JSON data):\n" + json.dumps(
        {**data, "fields": fields}, ensure_ascii=False)


def _review(extraction, *, malformed_first=False):
    calls = []

    def reviewer(system, user, **_kwargs):
        calls.append((system, user))
        if malformed_first and len(calls) == 1:
            return {"field_audits": []}, {"in": 1, "out": 1}
        return canned_grounding_verdict(user), {"in": 100, "out": 100}
    review_extraction(SOURCE, extraction, ROSTER, complete=reviewer, repair_invalid=True)
    return calls


def test_every_batch_sends_the_same_text_as_before_with_its_shared_prefixes_marked():
    extraction = _extraction()
    calls = _review(extraction)
    assert len(calls) > 1                                  # a review of several batches
    chapter_and_roster, with_extraction = set(), set()
    for system, user in calls:
        assert str(system) == EXTRACTION_REVIEW_SYSTEM
        assert system.reusable_ends == (len(EXTRACTION_REVIEW_SYSTEM),)
        fields = review_payload(user)["fields"]
        assert str(user) == before_this_change(SOURCE, extraction, ROSTER, fields)
        first, second = user.reusable_ends
        assert second == user.reusable_end
        chapter_and_roster.add(user[:first])
        with_extraction.add(user[:second])
        # The marks fall exactly between the chapter and roster, the extraction, and the fields.
        assert user[first:].startswith(', "extraction": ')
        assert user[second:].startswith(', "fields": ')
        assert '"fields"' not in user[:second]
    assert len(chapter_and_roster) == 1 and len(with_extraction) == 1
    prefix = chapter_and_roster.pop()
    assert prefix.startswith(HEAD + '{"source_passages": ') and '"prior_visible_identities"' in prefix
    assert '"extraction"' not in prefix


def test_a_fresh_extraction_after_a_grounding_repair_shares_the_chapter_and_roster_part():
    first = _review(_extraction())
    again = _review(_extraction("Thirty readers stood waiting in the hall."))
    a, b = first[0][1], again[0][1]
    assert a[:a.reusable_ends[0]] == b[:b.reusable_ends[0]]
    assert a[:a.reusable_end] != b[:b.reusable_end]


def test_a_format_repair_marks_only_the_instructions_every_review_shares():
    calls = _review(_extraction(), malformed_first=True)
    (malformed, _), (repair, user), *_ = calls
    assert str(malformed) == EXTRACTION_REVIEW_SYSTEM
    assert str(repair) == EXTRACTION_REVIEW_SYSTEM + _REVIEW_FORMAT_REPAIR
    assert repair.reusable_ends == (len(EXTRACTION_REVIEW_SYSTEM),)
    assert str(user) == str(calls[0][1])


def test_the_native_request_splits_the_same_text_into_parts_at_the_marks():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        user = "".join(part["text"] for part in body["messages"][1]["content"])
        return httpx.Response(200, json={
            "id": "t", "object": "chat.completion", "created": 0, "model": body["model"],
            "service_tier": "default",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(canned_grounding_verdict(user))}}],
            "usage": {"prompt_tokens": 7000, "completion_tokens": 500, "total_tokens": 7500}})
    client = LLMClient(provider="openai-compatible", api_key="synthetic", env={},
                       http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    sent = []

    def complete(system, user, *, tier, schema):
        sent.append((system, user))
        return client.complete(system, user, tier=tier, schema=schema)
    review_extraction(SOURCE, _extraction(), ROSTER, complete=complete)
    assert len(bodies) == len(sent) > 1
    for body, (system, user) in zip(bodies, sent, strict=True):
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        system_parts, user_parts = body["messages"][0]["content"], body["messages"][1]["content"]
        assert system_parts == [{"type": "text", "text": EXTRACTION_REVIEW_SYSTEM,
                                 "prompt_cache_breakpoint": {"mode": "explicit"}}]
        assert "".join(part["text"] for part in user_parts) == str(user)
        assert [("prompt_cache_breakpoint" in part) for part in user_parts] == [True, True, False]
        assert body["response_format"]["json_schema"]["name"] == ExtractionVerdict.__name__
        assert body["service_tier"] == "default" and body["reasoning_effort"] == "medium"
    # Up to the second mark every batch's request is the same: the cache's reusable prefix.
    assert len({json.dumps(body["messages"][1]["content"][:2]) for body in bodies}) == 1
