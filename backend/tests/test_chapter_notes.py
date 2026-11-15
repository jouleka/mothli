"""Mothli's chapter notes: written only from fully read chapters, checked sentence by sentence,
and given to a reply only when the whole chapter is inside that reply's read position."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_page_read_frontier import page_env as page_env, confirm  # noqa: E402,F401

from app.chapter_notes import build_missing  # noqa: E402

NOTE = "Mara put the cobalt key in a red tin."


def models(app, monkeypatch, *, notes="", reply="", unsupported=(), reaction=False):
    """A writer that returns ``notes`` for a chapter and ``reply`` for a question; Jev doubts any
    sentence holding an ``unsupported`` word and the second look agrees with the doubt. With
    ``reaction`` Jev reads the reader's message as a reaction to the book."""
    seen = {"notes": [], "reply": [], "checked": []}

    def complete(system, user, tier="cheap", schema=None):
        if schema is not None:
            return {"supported": False}, {"in": 90, "out": 5}
        kind = "notes" if "CHAPTER TEXT" in user else "reply"
        seen[kind].append(user)
        return (notes if kind == "notes" else reply), {"in": 100, "out": 40}

    def evaluate(state, questions):
        if "sentence" in state:
            seen["checked"].append(state)
        bad = any(word in state.get("sentence", "") for word in unsupported)
        return {name: {"noul": .9} if name == "book_assertion" or (reaction and name == "book_reaction")
                else {"choice": "unestablished" if bad else "supported",
                      "probabilities": {"supported": .1 if bad else .95}} if q["type"] == "choice"
                else {"noul": .01} for name, q in questions.items()}, {"in": 50, "out": 5}

    monkeypatch.setattr(app.state.client, "complete", complete)
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    return seen


def build(app, bid, settings):
    state = app.state
    return build_missing(bid, state.catalog, state.store, settings, state.client, state.worker)


def finish_chapter_one(api, app, bid):
    app.state.catalog.set_position(bid, "epubcfi(/6/2)", 1)
    return api.get(f"/api/books/{bid}/read-frontier").json()


def test_notes_are_written_from_a_finished_chapter_and_only_checked_sentences_are_kept(page_env, monkeypatch):
    api, bid, app, settings = page_env
    finish_chapter_one(api, app, bid)
    seen = models(app, monkeypatch, notes=NOTE + " Jonas later sold it abroad.", unsupported=["abroad"])
    assert build(app, bid, settings) == {"chapters": 1, "kept": 1, "dropped": 1}
    assert [(n["atom"], n["sentences"], n["dropped"]) for n in app.state.catalog.get_chapter_notes(bid)] == [
        (1, [NOTE], 1)]
    # The writer saw the finished chapter and nothing of the unread one.
    assert len(seen["notes"]) == 1 and "cobalt key" in seen["notes"][0]
    assert "FUTURE_CHAPTER_CANARY" not in seen["notes"][0]
    # Each sentence was judged against passages of that same chapter.
    assert all("FUTURE_CHAPTER_CANARY" not in " ".join(s["passages"]) for s in seen["checked"])
    # A second pass finds nothing left to do and pays for nothing.
    assert build(app, bid, settings) == {"chapters": 0, "kept": 0, "dropped": 0}
    assert len(seen["notes"]) == 1


def test_a_chapter_the_reader_is_still_inside_gets_no_notes(page_env, monkeypatch):
    api, bid, app, settings = page_env
    confirm(api, bid)                                   # part of the way into chapter one
    seen = models(app, monkeypatch, notes=NOTE)
    assert build(app, bid, settings) == {"chapters": 0, "kept": 0, "dropped": 0}
    assert seen["notes"] == [] and app.state.catalog.get_chapter_notes(bid) == []


def ask(api, bid, point):
    response = api.post(f"/api/books/{bid}/ask/stream", json={"question": "What's happened so far?", "read_frontier": point})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    return [e["text"] for e in events if e["type"] == "sentence"]


def test_a_reply_gets_the_notes_only_when_the_whole_chapter_is_inside_its_read_position(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    settings.reader_story_notes = True
    inside = confirm(api, bid)                          # a position inside chapter one
    finished = finish_chapter_one(api, app, bid)
    seen = models(app, monkeypatch, notes="Corvus poisoned Mara at midnight.", reply="Corvus poisoned Mara.")
    build(app, bid, settings)
    seen["checked"].clear()

    assert ask(api, bid, finished) == ["Corvus poisoned Mara."]
    assert "Corvus poisoned Mara at midnight." in seen["reply"][-1]
    assert any("Corvus poisoned Mara at midnight." in " ".join(s["passages"]) for s in seen["checked"])

    # The same reader asking from the earlier position: the chapter's end is not in that request.
    seen["checked"].clear()
    ask(api, bid, inside)
    assert "Corvus" not in seen["reply"][-1]
    assert all("Corvus" not in " ".join(s["passages"]) for s in seen["checked"])


def test_notes_are_not_used_when_the_setting_is_off_or_the_chapter_text_has_changed(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    finished = finish_chapter_one(api, app, bid)
    seen = models(app, monkeypatch, notes="Corvus poisoned Mara at midnight.", reply="Mara hid a key.")
    build(app, bid, settings)
    ask(api, bid, finished)                             # setting off
    assert "Corvus poisoned Mara at midnight." not in seen["reply"][-1]
    settings.reader_story_notes = True
    note = app.state.catalog.get_chapter_notes(bid)[0]
    app.state.catalog.set_chapter_notes(bid, 1, source_hash="another-text", version=note["version"],
                                        sentences=note["sentences"], dropped=0)
    ask(api, bid, finished)
    assert "Corvus poisoned Mara at midnight." not in seen["reply"][-1]


def wait_for(condition, seconds=5):
    from time import monotonic, sleep
    end = monotonic() + seconds
    while monotonic() < end:
        if condition():
            return True
        sleep(.02)
    return False


def test_one_book_is_never_noted_twice_at_once_and_a_failure_does_not_stop_later_work():
    from threading import Event
    from app.chapter_notes import NotesBuilder
    started, release, runs = Event(), Event(), []

    def work(book_id):
        runs.append(book_id)
        started.set()
        release.wait(5)
        raise RuntimeError("provider down")

    builder = NotesBuilder(work)
    try:
        assert builder.ensure("b") is True and started.wait(5)
        assert builder.ensure("b") is False                 # already under way
        release.set()
        assert wait_for(lambda: builder.ensure("b")) and wait_for(lambda: len(runs) == 2)
    finally:
        builder.close()


def test_notes_stopped_by_a_refusal_carry_the_reason_store_nothing_and_charge_no_check(page_env, monkeypatch):
    """Refused checks filled the book's ledger until its notes were refused for cost instead."""
    import pytest
    from app.llm.jev import JevUnavailable
    from test_jev import provider
    api, bid, app, settings = page_env
    finish_chapter_one(api, app, bid)
    models(app, monkeypatch, notes=NOTE + " Jonas waited beside the forge.")
    app.state.client.jev = provider(402)
    with pytest.raises(JevUnavailable) as caught:
        build(app, bid, settings)
    assert caught.value.reason == "no_credits"
    catalog = app.state.catalog
    assert catalog.get_chapter_notes(bid) == []                 # nothing unchecked is kept
    assert not [c for c in catalog.get_costs(bid) if c["model"].startswith("jev")]
    assert catalog.get_cost_reservations(bid) == []


def test_notes_wait_a_few_minutes_after_the_provider_refuses_the_account(caplog):
    """Every page turn and reply restarted the notes, paying the writer and calling a provider
    that had already said the account has no credits."""
    from app.chapter_notes import NotesBuilder
    from app.llm.jev import JevUnavailable
    now, runs = [1000.0], []
    reasons = ["no_credits", "unavailable", "unauthorized"]

    def work(book_id):
        runs.append(book_id)
        raise JevUnavailable("Jev review unavailable", reason=reasons[len(runs) - 1],
                             detail="No credits" if len(runs) == 1 else None)

    builder = NotesBuilder(work, clock=lambda: now[0])
    try:
        with caplog.at_level("WARNING", logger="mothli.notes"):
            assert builder.ensure("b") is True and wait_for(lambda: "(no_credits" in caplog.text)
            # One line for the owner: the reason, and the provider's own words.
            [line] = [r.getMessage() for r in caplog.records if r.name == "mothli.notes"]
            assert "JevUnavailable" in line and "no_credits" in line and "No credits" in line
            # No book's notes start while the provider is known to refuse the account.
            assert builder.ensure("b") is False and builder.ensure("other") is False
            now[0] += 299
            assert builder.ensure("b") is False and runs == ["b"]
            now[0] += 2
            assert wait_for(lambda: builder.ensure("b")) and wait_for(lambda: "(unavailable" in caplog.text)
            # A failure that may pass on its own (a timeout, a server error) is tried again at once.
            assert wait_for(lambda: builder.ensure("b")) and wait_for(lambda: "(unauthorized" in caplog.text)
            assert builder.ensure("b") is False and len(runs) == 3
    finally:
        builder.close()


def test_finishing_a_chapter_gets_its_notes_written_in_the_background(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_story_notes = True
    models(app, monkeypatch, notes=NOTE)
    assert api.put(f"/api/books/{bid}/position", json={"cfi": "epubcfi(/6/2)", "offset": 10**7}).status_code == 200
    assert wait_for(lambda: [n["atom"] for n in app.state.catalog.get_chapter_notes(bid)] == [1, 2])


def test_a_question_catches_up_on_notes_after_it_has_been_answered(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_story_notes = True
    finished = finish_chapter_one(api, app, bid)
    seen = models(app, monkeypatch, notes=NOTE, reply="Mara hid a key.")
    ask(api, bid, finished)
    assert "YOUR NOTES" not in seen["reply"][0]          # the question did not wait for them
    assert wait_for(lambda: [n["atom"] for n in app.state.catalog.get_chapter_notes(bid)] == [1])


def test_no_notes_are_written_while_the_setting_is_off(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    finished = finish_chapter_one(api, app, bid)
    seen = models(app, monkeypatch, notes=NOTE, reply="Mara hid a key.")
    ask(api, bid, finished)
    api.put(f"/api/books/{bid}/position", json={"cfi": "epubcfi(/6/2)", "offset": 10**7})
    assert not wait_for(lambda: seen["notes"], seconds=.5)


def test_a_chapter_too_long_for_one_call_is_cut_into_parts_that_each_fit_and_lose_nothing():
    from app.chapter_notes import NOTES_SYSTEM, chapter_parts, notes_prompt
    from app.cost.limits import estimate_input_tokens
    chapter = {"ordinal": 1, "title": "Chapter I", "text": "The wind crossed the valley. " * 400}
    parts = chapter_parts(chapter, 9000)
    assert len(parts) == 2 and "".join(parts) == chapter["text"]
    crowded = ["Mara hid the key. " * 20] * 12            # the most earlier notes a prompt carries
    assert all(estimate_input_tokens(NOTES_SYSTEM, notes_prompt(chapter, part, crowded)) <= 9000
               for part in parts)
    assert chapter_parts(chapter, 60_000) == [chapter["text"]]


def test_a_long_books_notes_are_cut_to_a_budget_keeping_the_thread_and_what_the_question_is_about():
    from app.chapter_notes import select_notes
    notes = [{"ordinal": n, "title": f"Chapter {n}", "sentences": [
        f"Mara crossed valley {n}.", "Jonas mended the hayrick cart." if n == 17 else "Jonas mended a wheel.",
        f"Corvus sold lamp {n}."]} for n in range(1, 41)]
    assert select_notes(notes, "anything", budget=100_000) == notes        # a short book keeps them all
    chosen = select_notes(notes, "What became of the hayrick cart?", budget=1500)
    kept = {n["ordinal"]: n["sentences"] for n in chosen}
    assert sum(len(" ".join(s)) for s in kept.values()) <= 1500
    # The thread: the opening note of chapters spread across the whole book, first to last.
    assert [n["ordinal"] for n in chosen] == sorted(kept) and min(kept) == 1 and max(kept) >= 39
    assert all(sentences[0].startswith("Mara crossed valley") or n == 17 for n, sentences in kept.items())
    # What the question is about is kept even though it is not an opening note.
    assert "Jonas mended the hayrick cart." in kept[17]
    assert select_notes(notes, "What became of the hayrick cart?", budget=1500) == chosen     # the same every time
