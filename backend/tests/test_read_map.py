"""The read map: Mothli knows what the reader has read, in whatever order they read it, and
nothing else. Skipping ahead, jumping around, saying "I've read this" and reading again."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import epub_ncx  # noqa: E402
from test_chapter_notes import models, wait_for  # noqa: E402
from test_ingest import InlineExecutor  # noqa: E402

from app.chapter_notes import build_missing  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

PEOPLE = {1: "Mara", 2: "Corvus", 3: "Vex"}


def line(chapter, number):
    return f"{PEOPLE[chapter]} moved lamp {number} in part {chapter}."


@pytest.fixture
def map_env(tmp_path):
    """Three chapters of sixty distinct sentences, so any page can be named exactly."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    with TestClient(app) as api:
        epub = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
                          " ".join(line(n, i) for i in range(1, 61))) for n in (1, 2, 3)],
                        title="The Lamp Book", author="Ada Writer")
        bid = api.post("/api/books", files={"file": ("b.epub", epub, "application/epub+zip")}).json()["book_id"]
        assert api.get(f"/api/books/{bid}/read-frontier").json()["atom"] == 0     # opened, nothing read
        yield api, bid, app, settings


def anchor(chapter, number, *, after, epoch=0):
    quote = line(chapter, number)
    return {"atom": chapter, "quote": quote, "prefix": quote if after else "",
            "cfi": "epubcfi(/6/2)", "position_epoch": epoch}


def read_page(api, bid, chapter, first, last, *, epoch=0, to_chapter=None, expect=200):
    """Report the page from the start of sentence ``first`` to the end of sentence ``last``."""
    response = api.put(f"/api/books/{bid}/read-page", json={
        "start": anchor(chapter, first, after=False, epoch=epoch),
        "end": anchor(to_chapter or chapter, last, after=True, epoch=epoch)})
    assert response.status_code == expect, response.text
    return response.json()


def ask(api, bid, question="What just happened?", epoch=0):
    point = api.get(f"/api/books/{bid}/read-frontier").json()
    response = api.post(f"/api/books/{bid}/ask/stream", json={"question": question, "read_frontier": point})
    assert response.status_code == 200, response.text
    return [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]


def test_skipping_to_the_middle_gives_mothli_that_page_and_nothing_before_or_after(map_env, monkeypatch):
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply="Corvus moved lamp 31 in part 2.")
    read_page(api, bid, 2, 30, 34)
    events = ask(api, bid)
    prompt = seen["reply"][-1]
    assert line(2, 30) in prompt and line(2, 34) in prompt
    assert line(2, 29) not in prompt and line(2, 35) not in prompt
    assert "Mara" not in prompt and "Vex" not in prompt
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Corvus moved lamp 31 in part 2."]
    assert all("Mara" not in " ".join(s["passages"]) for s in seen["checked"])


def test_jumping_around_adds_up_and_a_jump_itself_adds_nothing(map_env, monkeypatch):
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply="Vex moved lamp 50 in part 3.")
    read_page(api, bid, 3, 50, 52)
    read_page(api, bid, 1, 5, 6)
    # The older chapter counter still jumps when the reader confirms a leap. That is not reading.
    app.state.catalog.set_position(bid, "epubcfi(/6/2)", 3)
    point = read_page(api, bid, 1, 7, 8)
    assert (point["atom"], point["available_chapter"]) == (1, 1)
    ask(api, bid, "What did Vex do with lamp 50?")
    prompt = seen["reply"][-1]
    assert all(line(*where) in prompt for where in ((3, 50), (3, 52), (1, 5), (1, 8)))
    assert line(1, 4) not in prompt and line(1, 9) not in prompt and "Corvus" not in prompt
    assert line(3, 49) not in prompt and line(3, 53) not in prompt
    # Two stretches of one chapter are not shown to the writer as if they ran together.
    read_page(api, bid, 1, 40, 41)
    ask(api, bid, "What did Mara do with lamp 8?")
    assert f"{line(1, 8)} [...] {line(1, 40)}" in seen["reply"][-1]


def test_a_page_report_that_is_too_large_or_cannot_be_placed_adds_nothing(map_env, monkeypatch):
    api, bid, app, _ = map_env
    read_page(api, bid, 1, 1, 60, to_chapter=3, expect=422)       # not neighbours
    read_page(api, bid, 2, 9, 3, expect=422)                      # ends before it starts
    api.put(f"/api/books/{bid}/read-page", json={
        "start": {**anchor(1, 1, after=False), "quote": "No such sentence."},
        "end": anchor(1, 2, after=True)}).status_code == 422
    from app import read_map
    monkeypatch.setattr(read_map, "PAGE_CHARS", 200)
    read_page(api, bid, 1, 1, 30, expect=422)
    assert api.get(f"/api/books/{bid}/read-frontier").json()["atom"] == 0
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, bid)
    assert all("lamp" not in prompt for prompt in seen["reply"])


def test_a_page_may_run_from_the_end_of_one_chapter_into_the_next(map_env, monkeypatch):
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply="Hello.")
    point = read_page(api, bid, 1, 59, 2, to_chapter=2)
    assert point["atom"] == 2
    ask(api, bid)
    prompt = seen["reply"][-1]
    assert line(1, 59) in prompt and line(1, 60) in prompt and line(2, 1) in prompt and line(2, 2) in prompt
    assert line(1, 58) not in prompt and line(2, 3) not in prompt


def test_notes_are_written_only_for_a_chapter_the_map_covers_whole(map_env, monkeypatch):
    api, bid, app, settings = map_env
    settings.reader_story_notes = True
    seen = models(app, monkeypatch, notes="Corvus moved lamps in part 2.", reply="Hello.")
    read_page(api, bid, 3, 1, 59)                                  # all of part 3 but its last sentence
    said = api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [2], "read": True})
    assert said.status_code == 200, said.text
    state = app.state
    assert build_missing(bid, state.catalog, state.store, settings, state.client, state.worker)["chapters"] == 1
    assert [n["atom"] for n in state.catalog.get_chapter_notes(bid)] == [2]
    assert "Vex" not in seen["notes"][0] and "Mara" not in seen["notes"][0]
    ask(api, bid)
    assert "Reading notes on Part 2" in seen["reply"][-1]
    # "I haven't read this" takes the chapter, and so its notes, away again.
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [2], "read": False})
    ask(api, bid)
    assert "Corvus" not in seen["reply"][-1]


def test_reading_again_keeps_what_was_read_and_reading_fresh_hides_it_until_read_again(map_env, monkeypatch):
    api, bid, app, _ = map_env
    seen = models(app, monkeypatch, reply="Mara moved lamp 2 in part 1.")
    read_page(api, bid, 1, 1, 3)
    ask(api, bid, "Poor Mara!")
    turns = lambda: [t["content"] for t in api.get(f"/api/books/{bid}/conversation").json()["turns"]]  # noqa: E731
    assert turns() == ["Poor Mara!", "Mara moved lamp 2 in part 1."]

    again = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    assert again.status_code == 200, again.text
    assert turns() == ["Poor Mara!", "Mara moved lamp 2 in part 1."]
    ask(api, bid, epoch=1)
    assert line(1, 2) in seen["reply"][-1] and "Poor Mara!" in seen["reply"][-1]

    fresh = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 1, "fresh": True})
    assert fresh.status_code == 200, fresh.text
    assert turns() == []
    app.state.catalog.set_position(bid, "epubcfi(/6/2)", 3, expected_epoch=2)     # a leap, not reading
    before = len(seen["reply"])
    ask(api, bid, epoch=2)
    assert all("lamp" not in prompt and "Poor Mara" not in prompt for prompt in seen["reply"][before:])
    read_page(api, bid, 1, 1, 3, epoch=2)
    assert turns()[:2] == ["Poor Mara!", "Mara moved lamp 2 in part 1."]


def test_what_the_reader_had_already_read_is_carried_into_the_map_once(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    with TestClient(app) as api:
        epub = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}",
                          " ".join(line(n, i) for i in range(1, 61))) for n in (1, 2, 3)])
        bid = api.post("/api/books", files={"file": ("b.epub", epub, "application/epub+zip")}).json()["book_id"]
        app.state.catalog.set_position(bid, "epubcfi(/6/2)", 1)        # part 1 finished before the map
        settings.reader_read_map = True
        assert api.get(f"/api/books/{bid}/read-frontier").json()["atom"] == 1
        app.state.catalog.set_position(bid, "epubcfi(/6/4)", 3)        # later: a leap to the end
        seen = models(app, monkeypatch, reply="Hello.")
        ask(api, bid)
        assert line(1, 60) in seen["reply"][-1] and "Corvus" not in seen["reply"][-1]


def test_the_profile_follows_the_map(map_env):
    api, bid, _, _ = map_env
    book = lambda: api.get("/api/reader/profile").json()["books"][0]  # noqa: E731
    assert (book()["status"], book()["chapters_read"]) == ("unread", 0)
    read_page(api, bid, 2, 30, 34)
    assert (book()["status"], book()["chapters_read"]) == ("reading", 0)
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 3], "read": True})
    assert (book()["status"], book()["chapters_read"]) == ("reading", 2)
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [2], "read": True})
    assert (book()["status"], book()["chapters_read"]) == ("finished", 3)


def test_the_map_and_notes_follow_streamed_replies_unless_set():
    jev = dict(_env_file=None, reader_page_context=True, jev_enabled=True, reader_conversation_jev=True)
    off = Settings(_env_file=None)
    assert (off.reader_read_map, off.reader_story_notes) == (False, False)
    on = Settings(**jev)
    assert (on.reader_read_map, on.reader_story_notes) == (True, True)
    assert Settings(**jev, reader_read_map=False).reader_read_map is False
    assert Settings(**jev, reader_story_notes=False).reader_story_notes is False
    # Streamed replies turned off: the map and notes, which only they use, stay off and the app starts.
    reviewed = Settings(**jev, reader_conversation_stream=False)
    assert (reviewed.reader_read_map, reviewed.reader_story_notes) == (False, False)
    with pytest.raises(ValueError):
        Settings(_env_file=None, reader_read_map=True)


def test_the_map_is_off_unless_enabled(map_env):
    api, bid, _, settings = map_env
    settings.reader_read_map = False
    read_page(api, bid, 1, 1, 2, expect=404)
    assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1], "read": True}).status_code == 404


def test_a_finished_chapter_still_gets_notes_in_the_background(map_env, monkeypatch):
    api, bid, app, settings = map_env
    settings.reader_story_notes = True
    models(app, monkeypatch, notes="Mara moved lamps in part 1.")
    opening = {**anchor(1, 1, after=False), "quote": "Part 1 " + line(1, 1)}       # the heading is text too
    assert api.put(f"/api/books/{bid}/read-page", json={
        "start": opening, "end": anchor(1, 60, after=True)}).status_code == 200
    assert wait_for(lambda: [n["atom"] for n in app.state.catalog.get_chapter_notes(bid)] == [1])


def test_the_reader_app_can_see_how_much_of_each_chapter_is_read(map_env):
    api, bid, _, settings = map_env
    chapters = lambda: {c["atom"]: c["read"] for c in api.get(f"/api/books/{bid}/read-map").json()["chapters"]}  # noqa: E731
    assert chapters() == {1: 0, 2: 0, 3: 0}
    read_page(api, bid, 2, 30, 34)
    assert 0 < chapters()[2] < 1 and chapters()[1] == 0
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1], "read": True})
    assert chapters()[1] == 1
    settings.reader_read_map = False
    assert api.get(f"/api/books/{bid}/read-map").status_code == 404


def offer(api, bid):
    response = api.get(f"/api/books/{bid}/read-offer")
    assert response.status_code == 200, response.text
    return response.json()["offer"]


def test_a_book_finished_in_another_app_is_offered_as_read_and_only_the_readers_yes_makes_it_so(map_env, monkeypatch):
    api, bid, app, _ = map_env
    assert offer(api, bid) is None                                  # nothing recorded anywhere
    api.post("/api/history/import", json={"source": "goodreads", "books": [
        {"title": "The Lamp Book", "author": "Ada Writer", "status": "read", "finished_on": "2021-05-03"}]})
    assert offer(api, bid) == {"kind": "finished", "sources": ["goodreads"], "finished_on": "2021-05-03",
                               "chapter": None, "atoms": [1, 2, 3]}
    # The import alone gives Mothli nothing.
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, bid)
    assert all("lamp" not in prompt for prompt in seen["reply"])
    said = api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2, 3], "read": True, "source": "import"})
    assert said.status_code == 200
    assert api.get("/api/reader/profile").json()["books"][0]["status"] == "finished"
    assert offer(api, bid) is None                                  # asked once; the map is no longer empty


def test_highlights_from_another_app_place_the_reader_at_the_start_of_the_furthest_chapter(map_env):
    api, bid, _, _ = map_env
    api.post("/api/history/import", json={"source": "kindle", "highlights": [
        {"title": "The Lamp Book", "author": "Ada Writer", "text": line(1, 5) + " " + line(1, 6), "position": 5},
        {"title": "The Lamp Book", "author": "Ada Writer", "text": line(3, 40) + " " + line(3, 41), "position": 900},
        {"title": "The Lamp Book", "author": "Ada Writer", "text": "A line that is in no chapter of this edition.",
         "position": 950}]})
    # The furthest highlight found in this text is in part 3: parts 1 and 2 are behind the reader.
    assert offer(api, bid) == {"kind": "progress", "sources": ["kindle"], "finished_on": None,
                               "chapter": 3, "atoms": [1, 2]}
    read_page(api, bid, 1, 1, 2)
    assert offer(api, bid) is None                                  # the reader has started here instead


# --- the older chapter counter, which the cast, recap and chapter titles follow --------------------
def counter(api, bid):
    return api.get(f"/api/books/{bid}/position").json()["bookmark"]


def titles(api, bid):
    return [atom["title"] for atom in api.get(f"/api/books/{bid}/manifest").json()["atoms"]]


def heading(chapter, *, epoch=0):
    """The start of a chapter as the reader app reports it: the heading is text too."""
    return {**anchor(chapter, 1, after=False, epoch=epoch), "quote": f"Part {chapter} " + line(chapter, 1)}


def read_from_heading(api, bid, chapter, last):
    response = api.put(f"/api/books/{bid}/read-page", json={
        "start": heading(chapter), "end": anchor(chapter, last, after=True)})
    assert response.status_code == 200, response.text


def test_the_chapter_counter_follows_the_map_and_a_leap_no_longer_moves_it(map_env):
    api, bid, _, _ = map_env
    leap = api.put(f"/api/books/{bid}/position", json={"cfi": "epubcfi(/6/6)", "offset": 10**7})
    assert leap.status_code == 200 and leap.json()["bookmark"] == 0 and counter(api, bid) == 0
    read_page(api, bid, 3, 10, 12)                       # reading at the place leapt to
    assert counter(api, bid) == 0
    assert titles(api, bid) == ["Part 1", "", "Part 3"]  # the next chapter, and one being read
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2], "read": True})
    assert counter(api, bid) == 2 and titles(api, bid) == ["Part 1", "Part 2", "Part 3"]
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1], "read": False})      # "I haven't read this"
    assert counter(api, bid) == 0


def test_a_chapter_counts_as_finished_when_most_of_it_and_its_ending_were_read(map_env):
    api, bid, _, _ = map_env
    read_from_heading(api, bid, 1, 20)
    read_page(api, bid, 1, 30, 58)                       # one skimmed page in the middle, ending not reached
    assert counter(api, bid) == 0
    read_page(api, bid, 1, 59, 60)                       # the ending
    assert counter(api, bid) == 1
    read_from_heading(api, bid, 2, 10)
    read_page(api, bid, 2, 50, 60)                       # the ending, but most of the chapter skipped
    assert counter(api, bid) == 1


def test_reading_again_keeps_the_counter_and_reading_fresh_resets_it(map_env):
    api, bid, _, _ = map_env
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2, 3], "read": True})
    assert counter(api, bid) == 3
    again = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 0})
    assert again.json()["bookmark"] == 3 and counter(api, bid) == 3
    fresh = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": 1, "fresh": True})
    assert fresh.json()["bookmark"] == 0 and counter(api, bid) == 0 and titles(api, bid) == ["Part 1", "", ""]


def test_the_older_page_report_is_answered_from_the_map_instead_of_failing_after_a_leap(map_env):
    api, bid, _, _ = map_env
    reply = api.put(f"/api/books/{bid}/read-frontier", json=anchor(3, 40, after=False))
    assert reply.status_code == 200 and reply.json()["atom"] == 0          # nothing was read by reporting it
    read_page(api, bid, 3, 40, 41)
    assert api.put(f"/api/books/{bid}/read-frontier", json=anchor(3, 42, after=False)).json()["atom"] == 3


def test_help_with_a_selected_passage_works_wherever_the_reader_is(map_env, monkeypatch):
    api, bid, app, _ = map_env
    models(app, monkeypatch, reply="Hello.")
    body = {"action": "explain", "text": line(3, 40), "atom": 3, "cfi": "epubcfi(/6/6)"}
    there = api.post(f"/api/books/{bid}/selection-action", json=body)
    assert "outside the current reading position" not in there.text
    wrong = api.post(f"/api/books/{bid}/selection-action", json={**body, "text": "Not in this chapter at all."})
    assert wrong.status_code == 422


def test_the_shelf_can_say_which_chapter_the_reader_is_in_even_when_none_is_finished(map_env):
    api, bid, _, _ = map_env
    assert api.get(f"/api/books/{bid}/position").json()["place_chapter"] == 0
    read_page(api, bid, 3, 10, 12)
    position = api.get(f"/api/books/{bid}/position").json()
    assert (position["bookmark"], position["place_chapter"]) == (0, 3)


# --- chapters the reader went past: Mothli asks, and offers to count them --------------------------
def test_settling_into_a_chapter_past_unread_ones_brings_an_offer_to_count_them(map_env):
    api, bid, _, _ = map_env
    assert read_page(api, bid, 1, 1, 3)["skipped"] is None                  # reading from the start
    landed = read_page(api, bid, 3, 10, 12)
    assert landed["skipped"] == {"atoms": [1, 2], "chapters": "chapters 1 and 2", "skimmed": []}
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2], "read": True})
    assert read_page(api, bid, 3, 13, 14)["skipped"] is None


def test_a_reply_that_says_mothli_was_not_there_carries_the_offer_and_other_replies_do_not(map_env, monkeypatch):
    api, bid, app, _ = map_env
    read_page(api, bid, 3, 10, 12)
    models(app, monkeypatch, reply="I wasn’t with you for chapters 1 and 2. Vex moved lamp 11 in part 3.")
    assert ask(api, bid, "catch me up")[-1]["answer"]["offers"] == [
        {"book_id": bid, "title": "The Lamp Book", "atoms": [1, 2], "chapters": "chapters 1 and 2"}]
    models(app, monkeypatch, reply="Vex moved lamp 11 in part 3.")
    assert "offers" not in ask(api, bid, "What did Vex do?")[-1]["answer"]


def test_saying_chapters_were_read_starts_their_extraction_at_once(map_env, monkeypatch):
    api, bid, app, settings = map_env
    queued = []
    monkeypatch.setattr(app.state.worker, "enqueue", lambda book_id, through: queued.append((book_id, through)))
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2], "read": True}).raise_for_status()
    assert queued == [(bid, 2)]                       # no page turn needed for the cast to catch up
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [2], "read": False}).raise_for_status()
    assert queued == [(bid, 2)]                       # taking a chapter back extracts nothing new
    settings.reader_memory_ingest = False
    api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [2], "read": True}).raise_for_status()
    assert queued == [(bid, 2)]
