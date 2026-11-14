"""The streamed reply path: nothing is shown before it is checked, and doubt only shortens."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_page_read_frontier import page_env as page_env, confirm  # noqa: E402,F401

from app.ask_evidence import AskEvidenceError  # noqa: E402
from app.stream_conversation import (  # noqa: E402
    gate_sentences, sentence_cleared, sentence_packet, take_sentences)


def test_titles_initials_and_unfinished_text_do_not_end_a_sentence():
    assert take_sentences('Mr. Darcy is proud. He said "No." Then F. P. left! And') == (
        ["Mr. Darcy is proud.", 'He said "No."', "Then F. P. left!"], " And")
    assert take_sentences("It cost a lot. Yes", final=True) == (["It cost a lot.", "Yes"], "")
    assert take_sentences("Not finished yet.") == ([], "Not finished yet.")


def answers(*, story=.9, support=.95, choice="supported", added=.01):
    _, questions = sentence_packet("s", (), ["p"], question="q")
    return {name: {"noul": story} if name == "book_assertion"
            else {"choice": choice, "probabilities": {"supported": support}} if name == "relation"
            else {"noul": added} for name in questions}


@pytest.mark.parametrize("value,cleared", [
    (answers(), True), (answers(story=.02, support=.1, choice="unestablished"), True),
    (answers(support=.79), False), (answers(choice="unestablished"), False),
    (answers(added=.3), False), (answers(story=.1, support=.2), False)])
def test_a_sentence_is_cleared_only_without_story_content_or_with_full_support(value, cleared):
    assert sentence_cleared(value) is cleared


def test_a_malformed_judgment_clears_nothing():
    broken = answers()
    broken["relation"]["probabilities"] = {}
    with pytest.raises(AskEvidenceError):
        sentence_cleared(broken)


def run_gate(text, check, safe=lambda sentence: True):
    with ThreadPoolExecutor(max_workers=4) as pool:
        events = list(gate_sentences(iter([text[:7], text[7:]]), check, safe, pool=pool))
    return [e[1] for e in events if e[0] == "sentence"], events[-1]


def test_a_doubted_sentence_is_dropped_and_later_ones_are_judged_again_without_it():
    seen = []
    def check(sentence, earlier):
        seen.append((sentence, earlier))
        return "marries" not in sentence and not (sentence.startswith("He") and not earlier)
    shown, end = run_gate("Jonas took the key. He later marries Mara. He kept it. What struck you?", check)
    assert shown == ["Jonas took the key.", "He kept it.", "What struck you?"]
    assert end == ("end", shown, 1)
    # The sentences after the dropped one were judged again against what was actually shown.
    assert ("He kept it.", ("Jonas took the key.",)) in seen


def test_the_deterministic_gate_can_drop_a_sentence_the_model_cleared():
    shown, end = run_gate("Mara hid the key. Smerdyakov watched.", lambda s, e: True,
                          safe=lambda s: "Smerdyakov" not in s)
    assert shown == ["Mara hid the key."] and end[2] == 1


def test_a_long_reply_is_cut_at_the_sentence_limit():
    shown, end = run_gate(" ".join(f"Point {i}." for i in range(12)), lambda s, e: True)
    assert len(shown) == 8 and end[2] == 4


def events_of(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def stream_models(app, monkeypatch, reply, *, unsupported=(), down=False):
    def complete(system, user, tier="cheap", schema=None):
        assert schema is None and "SOURCE PASSAGES" in user
        return reply, {"in": 100, "out": 40}

    def evaluate(state, questions):
        if down:
            from app.llm.jev import JevUnavailable
            raise JevUnavailable("down")
        bad = any(word in state.get("sentence", "") for word in unsupported)
        chat = "?" in state.get("sentence", "")
        return {name: {"noul": .01 if chat else .9} if name == "book_assertion"
                else {"choice": "unestablished" if bad else "supported",
                      "probabilities": {"supported": .1 if bad else .95}} if q["type"] == "choice"
                else {"noul": .01} for name, q in questions.items()}, {"in": 50, "out": 5}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)


def test_stream_shows_checked_sentences_in_order_and_withholds_the_doubted_one(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin. She later sold it abroad. "
                  "What did you make of that?", unsupported=["abroad"])
    response = api.post(f"/api/books/{bid}/ask/stream", json={"question": "What did Mara do?", "read_frontier": point})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = events_of(response)
    assert [e["text"] for e in events if e["type"] == "sentence"] == [
        "Mara put the cobalt key in a red tin.", "What did you make of that?"]
    answer = events[-1]["answer"]
    assert events[-1]["type"] == "done" and "abroad" not in response.text
    assert answer["dropped_sentences"] == 1 and answer["style"] == "prose" and not answer["insufficient_evidence"]
    assert [c["text"] for c in answer["claims"]] == [e["text"] for e in events[:-1]]
    assert answer["read_frontier"]["id"] == point["id"]


def test_stream_with_nothing_cleared_ends_empty_and_an_outage_shows_nothing(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    stream_models(app, monkeypatch, "Mara sold it abroad.", unsupported=["abroad"])
    body = {"question": "What did Mara do?", "read_frontier": point}
    events = events_of(api.post(f"/api/books/{bid}/ask/stream", json=body))
    assert [e["type"] for e in events] == ["done"] and events[0]["answer"]["insufficient_evidence"]
    stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin.", down=True)
    response = api.post(f"/api/books/{bid}/ask/stream", json=body)
    events = events_of(response)
    assert [e["type"] for e in events] == ["error"] and events[0]["status"] == 503 and "cobalt" not in response.text


def test_stream_is_off_unless_enabled(page_env):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    assert api.post(f"/api/books/{bid}/ask/stream",
                    json={"question": "What did Mara do?", "read_frontier": point}).status_code == 404
