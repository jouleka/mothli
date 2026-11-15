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
    gate_sentences, sentence_blocked, sentence_cleared, sentence_packet, take_sentences)


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
    shown, end = run_gate(" ".join(f"Point {i}." for i in range(15)), lambda s, e: True)
    assert shown == [f"Point {i}." for i in range(12)] and end[2] == 3


def events_of(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def stream_models(app, monkeypatch, reply, *, unsupported=(), down=False, second=None, sure=()):
    """``unsupported`` words make Jev doubt a sentence; ``second`` maps a word to the second
    look's verdict (default: it agrees with the doubt); ``sure`` words make Jev certain."""
    looked = []

    def complete(system, user, tier="cheap", schema=None):
        if schema is not None:
            assert schema.__name__ == "SentenceVerdict" and "SOURCE PASSAGES" not in user
            sentence = json.loads(user)["sentence"]
            looked.append(sentence)
            return {"supported": any(ok and word in sentence for word, ok in (second or {}).items())}, {"in": 90, "out": 5}
        assert "SOURCE PASSAGES" in user
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
                else {"noul": .95 if name == "dropped_condition" and any(
                    word in state.get("sentence", "") for word in sure) else .01}
                for name, q in questions.items()}, {"in": 50, "out": 5}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    return looked


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


def stream_record(caplog):
    records = [(r.levelname, json.loads(r.getMessage())) for r in caplog.records if r.name == "mothli.conversation"]
    return [(level, record) for level, record in records if record["event"] == "conversation-stream"]


def test_a_refused_check_tells_the_owner_why_charges_nothing_and_shows_nothing(page_env, monkeypatch, caplog):
    """"No credits" surfaced only as JevUnavailable, and every refused check was charged in full."""
    from test_jev import NO_CREDITS, provider
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin. Jonas waited beside the forge.")
    app.state.client.jev = provider(402)
    with caplog.at_level("WARNING", logger="mothli.conversation"):
        response = api.post(f"/api/books/{bid}/ask/stream",
                            json={"question": "What did Mara do?", "read_frontier": point})
    # The reader sees the same words as for any outage, no billing details, and no unchecked sentence.
    assert events_of(response) == [{"type": "error", "status": 503, "reason": "no_credits",
                                    "message": "Jev review unavailable; no unchecked reply was published"}]
    assert "cobalt" not in response.text and "organization" not in response.text
    # One warning line for the owner, naming the reason.
    [(level, record)] = stream_record(caplog)
    assert level == "WARNING" and record["failure"] == "JevUnavailable"
    assert record["reason"] == "no_credits" and record["provider_message"] == NO_CREDITS
    assert "private-test-key" not in caplog.text + response.text
    # No refused check is left in the book's ledger, settled or pending.
    catalog = app.state.catalog
    assert not [c for c in catalog.get_costs(bid) if c["model"].startswith("jev")]
    assert catalog.get_cost_reservations(bid) == []


def test_an_outage_of_unknown_cause_is_named_unavailable_and_stays_charged(page_env, monkeypatch, caplog):
    """The rule for unknown spend must survive: a failure that is not a refusal keeps its estimate."""
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    stream_models(app, monkeypatch, "Mara put the cobalt key in a red tin.", down=True)
    with caplog.at_level("WARNING", logger="mothli.conversation"):
        events = events_of(api.post(f"/api/books/{bid}/ask/stream",
                                    json={"question": "What did Mara do?", "read_frontier": point}))
    assert [(e["type"], e["status"], e["reason"]) for e in events] == [("error", 503, "unavailable")]
    [(level, record)] = stream_record(caplog)
    assert level == "WARNING" and record["reason"] == "unavailable" and "provider_message" not in record
    failed = [c["phase"] for c in app.state.catalog.get_costs(bid) if c["model"].startswith("jev")]
    assert failed and all(phase.endswith("-failed-reserved") for phase in failed)


def test_streaming_follows_the_jev_conversation_setting_unless_set():
    from app.config import Settings
    jev = dict(_env_file=None, reader_page_context=True, jev_enabled=True, reader_conversation_jev=True)
    assert Settings(_env_file=None).reader_conversation_stream is False
    assert Settings(**jev).reader_conversation_stream is True
    assert Settings(**jev, reader_conversation_stream=False).reader_conversation_stream is False
    with pytest.raises(ValueError):
        Settings(_env_file=None, reader_conversation_stream=True)


def test_stream_is_off_unless_enabled(page_env):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_stream = False
    assert api.post(f"/api/books/{bid}/ask/stream",
                    json={"question": "What did Mara do?", "read_frontier": point}).status_code == 404


def test_a_dropped_condition_or_misplaced_event_jev_is_sure_of_cannot_be_overruled():
    assert sentence_blocked(answers(added=.01)) is False
    sure = answers()
    sure["dropped_condition"]["noul"] = .7
    assert sentence_blocked(sure) is True
    placed = answers()
    placed["unestablished_location"]["noul"] = .45
    assert sentence_blocked(placed) is True


def test_a_doubted_sentence_gets_one_second_look_which_alone_decides(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    looked = stream_models(
        app, monkeypatch, "Mara put the cobalt key in a red tin. She seems careful with it. "
        "She later sold it abroad. She must return it at dawn.",
        unsupported=["careful", "abroad"], second={"careful": True, "abroad": False}, sure=["dawn"])
    response = api.post(f"/api/books/{bid}/ask/stream", json={"question": "What did Mara do?", "read_frontier": point})
    events = events_of(response)
    assert [e["text"] for e in events if e["type"] == "sentence"] == [
        "Mara put the cobalt key in a red tin.", "She seems careful with it."]
    # Cleared sentences and the one Jev is sure about never reach the second look.
    assert sorted(looked) == ["She later sold it abroad.", "She seems careful with it."]
    assert events[-1]["answer"]["dropped_sentences"] == 2 and "abroad" not in response.text and "dawn" not in response.text
    phases = [c["phase"] for c in events[-1]["answer"]["cost"]["calls"]]
    assert phases.count("stream-second-look") == 2


def test_a_streamed_reply_reads_the_book_alongside_other_requests_instead_of_locking_it():
    """Treated as a change to the book, it waited for every open read and blocked new ones."""
    import asyncio
    from app.deps import book_lifecycle

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(book_request_locks={}))
        def request(method, path):
            return SimpleNamespace(app=app, method=method, url=SimpleNamespace(path=path))
        reading = book_lifecycle("b", request("GET", "/api/books/b/epub"))
        await reading.__anext__()                      # a read that stays open
        streaming = book_lifecycle("b", request("POST", "/api/books/b/ask/stream"))
        await asyncio.wait_for(streaming.__anext__(), timeout=1)
        later = book_lifecycle("b", request("GET", "/api/books/b/read-frontier"))
        await asyncio.wait_for(later.__anext__(), timeout=1)
        for lease in (later, streaming, reading):
            with pytest.raises(StopAsyncIteration):
                await lease.__anext__()

    asyncio.run(scenario())


def test_a_long_recap_gets_six_second_looks_and_a_seventh_doubted_sentence_is_dropped(page_env, monkeypatch):
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    reply = " ".join(f"Mara seems careful with tin {i}." for i in range(7))
    looked = stream_models(app, monkeypatch, reply, unsupported=["careful"], second={"careful": True})
    events = events_of(api.post(f"/api/books/{bid}/ask/stream",
                                json={"question": "What did Mara do?", "read_frontier": point}))
    # Which of the seven goes without a look depends on timing. Whichever it is, six sentences
    # get one, those six are shown, and the ones after a dropped sentence are looked at again.
    assert len(set(looked)) == 6 and len([e for e in events if e["type"] == "sentence"]) == 6
    assert events[-1]["answer"]["dropped_sentences"] == 1


def test_a_dropped_sentence_does_not_cost_the_ones_after_it_their_second_look(page_env, monkeypatch):
    """Sentences after a dropped one are judged again without it. That second judgment used to
    count against the reply's allowance, so one drop early in a recap cut everything after it."""
    api, bid, app, settings = page_env
    point = confirm(api, bid)
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    reply = "Mara sold it abroad. " + " ".join(f"Mara seems careful with tin {i}." for i in range(5))
    stream_models(app, monkeypatch, reply, unsupported=["careful", "abroad"],
                  second={"careful": True, "abroad": False})
    events = events_of(api.post(f"/api/books/{bid}/ask/stream",
                                json={"question": "What did Mara do?", "read_frontier": point}))
    assert [e["text"] for e in events if e["type"] == "sentence"] == [
        f"Mara seems careful with tin {i}." for i in range(5)]
    assert events[-1]["answer"]["dropped_sentences"] == 1
