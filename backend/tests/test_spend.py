"""Nothing is paid for twice: how many times a chapter's notes are written and a sentence is
checked, in the app and in the scripts that run it against paid models. Stand-in models that
count their calls; nothing here reaches a provider."""
import json
import logging
import re
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import epub_ncx  # noqa: E402
from test_chapter_notes import wait_for  # noqa: E402
from test_ingest import InlineExecutor  # noqa: E402
from test_read_map import ask, line, read_page  # noqa: E402

from app import chapter_notes  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from scripts import companion_eval as ev  # noqa: E402

THROUGH_3 = {"name": "through-3", "atom": 3}


@contextmanager
def library(directory, *, changed=None):
    """A book of three chapters in its own library, with chapter notes on. ``changed`` is a
    chapter whose text differs from the same book in another library."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(directory))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    settings.reader_read_map = settings.reader_story_notes = True
    with TestClient(app) as api:
        epub = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}", " ".join(line(n, i) for i in range(1, 61))
                          + (" A line added in another edition." if n == changed else "")) for n in (1, 2, 3)],
                        title="The Lamp Book", author="Ada Writer")
        bid = api.post("/api/books", files={"file": ("b.epub", epub, "application/epub+zip")}).json()["book_id"]
        yield api, bid, app, settings


@pytest.fixture
def lamp(tmp_path):
    with library(tmp_path / "data") as env:
        yield env


def counting(app, *, reply="", notes=(1, 2, 3), doubt=(), agree=(), slow=0.0):
    """A writer whose notes on a chapter of the lamp book are that chapter's sentences ``notes``,
    and a Jev that doubts a sentence holding a ``doubt`` word; the second look passes one holding
    an ``agree`` word. Every call is counted: notes written per chapter (by its number in the
    lamp book, by its title in any other), checks and second looks per sentence."""
    count = {"written": Counter(), "checked": Counter(), "second": Counter(), "prompts": []}

    def complete(system, user, tier="cheap", schema=None):
        if schema is not None:
            sentence = json.loads(user)["sentence"]
            count["second"][sentence] += 1
            return {"supported": any(word in sentence for word in agree)}, {"in": 90, "out": 5}
        if "CHAPTER TEXT" not in user:
            count["prompts"].append(user)
            return reply, {"in": 100, "out": 40}
        title = re.search(r"CHAPTER TEXT \((.*)\):\n", user)[1]
        part = int(title[5:]) if title.startswith("Part ") else None
        count["written"][part or title] += 1
        time.sleep(slow)                    # a real writer is slower than anything the caller does
        return (" ".join(line(part, i) for i in notes) if part else "A family is described."), {"in": 100, "out": 40}

    def evaluate(state, questions):
        if "sentence" in state:
            count["checked"][state["sentence"]] += 1
        bad = any(word in state.get("sentence", "") for word in doubt)
        return {name: {"noul": .9} if name == "book_assertion"
                else {"choice": "unestablished" if bad else "supported",
                      "probabilities": {"supported": .1 if bad else .95}} if q["type"] == "choice"
                else {"noul": .01} for name, q in questions.items()}, {"in": 50, "out": 5}

    app.state.client.complete = complete
    app.state.client.jev = SimpleNamespace(model="jev-1.14.0", evaluate=evaluate, close=lambda: None)
    return count


def paid(count, notes=(1, 2, 3)):
    """For each chapter: (times its notes were written, times a sentence of them was checked)."""
    return {part: (count["written"][part], sum(count["checked"][line(part, i)] for i in notes))
            for part in (1, 2, 3)}


def idle(app):
    assert wait_for(lambda: not app.state.notes_builder._queued)


def mark(api, bid, atoms, read=True):
    assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": atoms, "read": read}).status_code == 200


def test_the_app_writes_and_checks_a_finished_chapter_once_however_often_it_hears_of_it(lamp):
    """Catches: a second writer for a chapter. One started while its notes are being written
    (two reports close together), or one that writes notes again that are already held (after
    a reply, after "read it fresh", after "I haven't read this" and reading it again)."""
    api, bid, app, _settings = lamp
    count = counting(app, reply=line(1, 30), slow=.1)
    mark(api, bid, [1])                                             # "I've read this"
    idle(app)
    assert paid(count) == {1: (1, 3), 2: (0, 0), 3: (0, 0)}
    end = {"atom": 2, "quote": line(2, 60), "prefix": line(2, 60), "cfi": "epubcfi(/6/2)", "position_epoch": 0}
    assert api.put(f"/api/books/{bid}/read-page", json={                # the last page of chapter 2
        "start": {**end, "quote": "Part 2 " + line(2, 1), "prefix": ""}, "end": end}).status_code == 200
    idle(app)
    ask(api, bid)                                                   # a reply catches up on notes
    idle(app)
    assert paid(count) == {1: (1, 3), 2: (1, 3), 3: (0, 0)}
    mark(api, bid, [3])                                             # and now all at once,
    mark(api, bid, [3])                                             # while chapter 3 is being written
    ask(api, bid)
    mark(api, bid, [1, 2, 3])
    idle(app)
    epoch = app.state.catalog.get_state(bid)["position_epoch"]
    api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": epoch, "fresh": True}).raise_for_status()
    mark(api, bid, [1, 2, 3])                                       # read it fresh, then read again
    idle(app)
    mark(api, bid, [2], read=False)                                 # "I haven't read this", then read again
    mark(api, bid, [2])
    ask(api, bid)
    idle(app)
    assert paid(count) == {1: (1, 3), 2: (1, 3), 3: (1, 3)}
    assert not count["second"]


def test_a_sentence_is_checked_once_and_again_only_when_one_before_it_was_dropped(lamp):
    """Catches: more checks than a reply or a chapter's notes need. One Jev check per sentence;
    a second look only for a sentence Jev doubts; and one more of each only for a sentence
    written after a dropped one, which is judged again against what was actually shown."""
    api, bid, app, _settings = lamp
    read_page(api, bid, 1, 1, 30)
    four = [line(1, i) for i in (1, 2, 3, 4)]

    def reply(**models):
        count = counting(app, reply=" ".join(four), **models)
        shown = [e["text"] for e in ask(api, bid) if e["type"] == "sentence"]
        return shown, [count["checked"][s] for s in four], [count["second"][s] for s in four]

    assert reply() == (four, [1, 1, 1, 1], [0, 0, 0, 0])
    assert reply(doubt=["lamp 2 "], agree=["lamp 2 "]) == (four, [1, 1, 1, 1], [0, 1, 0, 0])
    kept = [four[0], *four[2:]]
    assert reply(doubt=["lamp 2 "]) == (kept, [1, 1, 2, 2], [0, 1, 0, 0])
    assert reply(doubt=["lamp 2 ", "lamp 3 "], agree=["lamp 3 "]) == (kept, [1, 1, 2, 2], [0, 1, 2, 0])
    # A chapter's notes go through the same gate: its first sentence dropped, the other two twice.
    count = counting(app, doubt=["lamp 1 "])
    mark(api, bid, [1])
    idle(app)
    assert [count["checked"][line(1, i)] for i in (1, 2, 3)] == [1, 2, 2] and count["written"][1] == 1


def run_notes(env, cache, position=THROUGH_3, check_budget=lambda: None):
    """What both scripts do with a book: take its notes from the cache, enter the position,
    and wait for the app to write whatever is still missing."""
    api, bid, app, settings = env
    chapters = app.state.worker.source_chapters(bid)
    cache.load(app.state.catalog, bid, chapters)
    ev.enter_by_map(api, app, bid, position, chapters)
    try:
        ev.wait_for_notes(app, settings, bid, check_budget, cache, pause=.02)
    finally:
        idle(app)


def test_the_scripts_leave_notes_to_the_apps_writer_so_a_chapter_is_paid_for_once(lamp, tmp_path):
    """Catches: the evaluation writing notes itself (``build_missing``) while marking the
    chapters read has already started the app's writer. Both found no notes and both wrote
    every chapter: two writer calls and twice the checks for each (audit 6t, measured in 6x)."""
    count = counting(lamp[2], slow=.3)
    run_notes(lamp, ev.NotesCache(lamp[3], path=tmp_path / "notes.json"))
    assert paid(count) == {1: (1, 3), 2: (1, 3), 3: (1, 3)}
    assert not hasattr(ev, "build_missing")


def test_a_second_run_takes_its_notes_from_the_cache_and_pays_for_none(lamp, tmp_path):
    """Catches: every run on a new temporary library writing and checking the same notes
    again. And loading the cache giving a reply notes on a chapter the reader has not read."""
    path = tmp_path / "notes.json"
    counting(lamp[2])
    first = ev.NotesCache(lamp[3], path=path)
    run_notes(lamp, first)
    assert first.summary().startswith("chapter notes: 0 chapters from the cache, 3 written")
    with library(tmp_path / "second") as env:
        api, bid, app, settings = env
        count, second = counting(app, reply=line(1, 30)), ev.NotesCache(settings, path=path)
        run_notes(env, second, {"name": "through-2", "atom": 2})
        ask(api, bid)
        idle(app)
        assert paid(count) == {1: (0, 0), 2: (0, 0), 3: (0, 0)}
        assert second.summary().startswith("chapter notes: 2 chapters from the cache, 0 written")
        [prompt] = count["prompts"]
        assert "Reading notes on Part 1: " + line(1, 1) in prompt and "Reading notes on Part 2" in prompt
        assert "Part 3" not in prompt and "Vex" not in prompt


def test_cached_notes_are_not_used_for_a_changed_chapter_another_version_or_another_model(
        lamp, tmp_path, monkeypatch):
    """Catches: notes written from other text, by an older notes prompt, or by other models
    being passed off as this run's."""
    path = tmp_path / "notes.json"
    counting(lamp[2])
    run_notes(lamp, ev.NotesCache(lamp[3], path=path))
    with library(tmp_path / "second", changed=2) as env:
        _api, bid, app, settings = env
        count, cache = counting(app), ev.NotesCache(settings, path=path)
        run_notes(env, cache)
        assert paid(count) == {1: (0, 0), 2: (1, 3), 3: (0, 0)}
        assert cache.summary().startswith("chapter notes: 2 chapters from the cache, 1 written")

    with library(tmp_path / "third") as env:
        _api, bid, app, settings = env
        chapters = app.state.worker.source_chapters(bid)

        def given():
            cache = ev.NotesCache(settings, path=path)
            cache.load(app.state.catalog, bid, chapters)
            return sorted(atom for _, atom in cache.loaded)

        for holder, name, other in ((chapter_notes, "VERSION", "chapter-notes-v2"),
                                    (settings, "jev_model", "jev-1.13.0"),
                                    (settings, "llm_large_model", "another-writer")):
            with monkeypatch.context() as patch:
                patch.setattr(holder, name, other)
                assert given() == [] and app.state.catalog.get_chapter_notes(bid) == []
        assert given() == [1, 2, 3]


def test_fresh_notes_writes_every_chapter_again_and_keeps_the_new_notes(lamp, tmp_path):
    """Catches: a run that measures the notes themselves measuring an earlier run's instead."""
    path = tmp_path / "notes.json"
    counting(lamp[2])
    run_notes(lamp, ev.NotesCache(lamp[3], path=path))
    with library(tmp_path / "second") as env:
        _api, _bid, app, settings = env
        count, cache = counting(app, notes=(7, 8)), ev.NotesCache(settings, fresh=True, path=path)
        run_notes(env, cache)
        assert paid(count, notes=(7, 8)) == {1: (1, 2), 2: (1, 2), 3: (1, 2)}
        assert cache.summary().startswith("chapter notes: 0 chapters from the cache, 3 written")
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert {note["sentences"][0] for note in kept.values()} == {line(n, 7) for n in (1, 2, 3)}


def test_passing_the_cap_stops_the_run_and_the_notes_writer_with_it(lamp, tmp_path, capsys):
    """Catches: a run with no ceiling on Jev spend, and a stop that ends the script's loop while
    the app's writer goes on to pay for the rest of the book."""
    api, bid, app, settings = lamp
    count = counting(app, slow=.3)
    cache = ev.NotesCache(settings, path=tmp_path / "notes.json")
    with ev.paid_run(app.state.catalog):                   # the cap stops the run without a traceback
        run_notes(lamp, cache, check_budget=ev.spend_stop(app.state.catalog, settings, 0.000001))
        pytest.fail("the run went on past the cap")
    assert count["written"][3] == 0
    assert 3 not in {note["atom"] for note in app.state.catalog.get_chapter_notes(bid)}
    spent = ev.jev_spend(app.state.catalog)
    assert spent > 0.000001
    printed = capsys.readouterr().out
    assert "stopped: Jev spend $" in printed and f"Jev spend: ${spent:.4f}" in printed
    # Nothing more can be paid for in this library, by a reply or by the notes writer.
    assert ask(api, bid)[-1]["status"] == 429
    assert ev.jev_spend(app.state.catalog) == spent


def test_the_spend_is_printed_when_a_run_finishes_and_under_the_cap_nothing_stops_it(lamp, tmp_path, capsys):
    """Catches: a report with no Jev total, and a cap that stops a run that is inside it."""
    _api, _bid, app, settings = lamp
    count = counting(app)
    with ev.paid_run(app.state.catalog):
        run_notes(lamp, ev.NotesCache(settings, path=tmp_path / "notes.json"),
                  check_budget=ev.spend_stop(app.state.catalog, settings, 0.10))
    assert paid(count) == {1: (1, 3), 2: (1, 3), 3: (1, 3)}
    printed = capsys.readouterr().out
    assert "stopped" not in printed and f"Jev spend: ${ev.jev_spend(app.state.catalog):.4f}" in printed
    assert "jev-1.14.0: 9 calls" in printed


def test_a_whole_evaluation_run_pays_for_each_chapter_once_and_a_second_run_for_none(
        tmp_path, monkeypatch, capsys, request):
    """The evaluation itself, start to end, on the Karamazov chapters with stand-in models: five
    positions, each entered from a forgotten book, and two questions.

    Catches: the script writing notes beside the app's writer, or again at each position; a
    second run paying for notes the first one wrote; and a mistake in the script that would
    otherwise first show part-way through a paid run."""
    import app.conversation_jev as jev_module
    import app.page_conversation as page
    import app.stream_conversation as stream
    for holder, name in ((stream, "gate_sentences"), (jev_module, "accepted_judgment"), (page, "review_with_jev"),
                         (page, "review_escalation"), (page, "review_draft")):
        monkeypatch.setattr(holder, name, getattr(holder, name))     # the run wraps these; put them back
    log = logging.getLogger("mothli.conversation")                   # and it listens to this log
    request.addfinalizer(lambda held=list(log.handlers), level=log.level: (
        log.setLevel(level), setattr(log, "handlers", held)))
    counts = []

    def settings_without_providers(**given):
        if "data_dir" not in given:                                  # the env file: no keys in it
            return SimpleNamespace(typesafe_api_key=None, openai_api_key=None, openai_base_url=None)
        return Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, **{name: given[name] for name in (
                            "data_dir", "llm_large_model", "jev_model", "reader_memory_ingest",
                            "reader_answer_cache", "reader_prepare_enabled", "classics_enabled")})

    def app_with_notes(settings):
        app = create_app(settings, ingest_executor=InlineExecutor())
        settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_story_notes = True
        return app

    class Client(TestClient):
        def __enter__(self):
            api = super().__enter__()
            counts.append(counting(self.app, reply="A family is described.", slow=.2))
            return api

    monkeypatch.setattr(ev, "Settings", settings_without_providers)
    monkeypatch.setattr(ev, "create_app", app_with_notes)
    monkeypatch.setattr(ev, "TestClient", Client)
    monkeypatch.setattr(ev, "NOTES_CACHE", tmp_path / "notes.json")
    monkeypatch.setattr(sys, "argv", ["companion_eval.py", "--env-file", "none", "--out", str(tmp_path / "eval.json"),
                                      "--books", "karamazov", "--styles", "stream", "--notes",
                                      "--only", "k3-recent", "k-whole-so-far"])
    ev.main()
    first = capsys.readouterr().out
    assert sorted(counts[0]["written"].values()) == [1, 1, 1, 1, 1]
    assert "chapter notes: 0 chapters from the cache, 5 written" in first
    # Every call of the run: five chapters of notes with one sentence each, and two questions.
    assert all(line in first for line in ("gpt-6.1-terra: 7 calls", "jev-1.14.0: 11 calls", "Jev spend: $"))
    assert "stopped" not in first
    assert [row["id"] for row in json.loads((tmp_path / "eval.json").read_text(encoding="utf-8"))] == [
        "k3-recent", "k-whole-so-far"]
    ev.main()
    assert not counts[1]["written"] and len(counts[1]["prompts"]) == 2
    assert "chapter notes: 5 chapters from the cache, 0 written" in capsys.readouterr().out
