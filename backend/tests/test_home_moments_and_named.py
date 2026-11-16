"""The home chat (6ah): a remembered moment can be said back, and a book the reader has named can be
suggested.

A note such as "The monk being sent away from Athos wrecked the reader." is the reader's word,
no evidence of what is in a book: a sentence saying the moment back stands only on the book's own
read text about it. The home chat now looks for that text by the note's own words, in read text
only. And a book the reader has named, in their message or in a plan they told Mothli, is put
first among the books Mothli may suggest when it passes every rule. Stand-in writer and Jev
throughout; each test names the break it catches.
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import epub_ncx  # noqa: E402
from test_home_conversation import ask, shelf as shelf  # noqa: E402,F401
from test_home_suggestions import block  # noqa: E402
from test_ingest import InlineExecutor  # noqa: E402
from test_recollections import models  # noqa: E402

from app import home_conversation, recollections, stream_conversation  # noqa: E402
from app.config import Settings  # noqa: E402
from app.home_conversation import HOME  # noqa: E402
from app.main import create_app  # noqa: E402
from app.recollections import BLOCK_HEAD, PASSAGE_HEAD  # noqa: E402

ATHOS = "The old monk Ferapont was sent away from Athos with nothing but his staff."
MOMENT = "The monk being sent away from Athos wrecked the reader."
WHICH = "do you remember which part got to me?"
REPLY = "That one stayed with you."


HALLS = {1: "The Gate", 2: "The Yard", 3: "The Bell"}


def monastery(chapter, number):
    """Plain lines, and in chapter 1 one moment that shares no word with ``WHICH``."""
    if chapter == 1 and number == 12:
        return ATHOS
    return f"Brother Ivo carried candle {number} through hall {chapter}."


def cloister_book(api):
    """Chapters named so that no title shares a word with ``WHICH`` ("Part 1" would)."""
    epub = epub_ncx([(f"c{n}.xhtml", HALLS[n], HALLS[n], " ".join(monastery(n, i) for i in range(1, 81)))
                     for n in (1, 2, 3)], title="The Cloister", author="Ada Writer")
    return api.post("/api/books", files={"file": ("cloister.epub", epub, "application/epub+zip")}).json()["book_id"]


@pytest.fixture
def cloister(tmp_path):
    """One book, chapters 1 and 2 read in full; the moment is in chapter 1."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    settings.reader_recollections = True
    with TestClient(app) as api:
        bid = cloister_book(api)
        api.get(f"/api/books/{bid}/read-frontier")
        assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2], "read": True}).status_code == 200
        yield api, app, settings, bid


def read_now(app):
    catalog = app.state.catalog
    return {item["book_id"]: [item["incarnation"],
                              catalog.get_read_map(item["book_id"], incarnation=item["incarnation"])]
            for item in catalog.list_books()}


def remember(app, text, *, kind="feeling", tied=True, book_id=None):
    """Store one note as distilling would, with what is read now as the record of what was read
    when it was said. ``book_id`` None and ``tied``: a feeling said on the home screen."""
    drew_on = {bid: record for bid, record in read_now(app).items() if record[1]} if tied else None
    if book_id is not None:
        drew_on = {book_id: drew_on[book_id]}
    app.state.catalog.keep_recollections([{
        "kind": kind, "text": text, "tied": tied, "book_id": book_id, "drew_on": drew_on, "said": "x",
        "said_at": "2026-11-16T09:00:00+00:00", "replaces": None}], cap=recollections.STORE_CAP)


def fresh_ask(app, monkeypatch, api, question, reply=REPLY):
    app.state.catalog.clear_turns(None)
    seen = models(app, monkeypatch, reply=reply)
    events = ask(api, question)
    return seen, events


def read_given(seen):
    """The read-text passages each check was given, and the writer's prompt."""
    return [[p for p in state["passages"] if p.startswith("From “")] for state in seen["checked"]], seen["reply"][-1]


def largest_check(seen):
    return max(sum(len(json.dumps(p, ensure_ascii=False).encode()) + 2 for p in state["passages"])
               for state in seen["checked"])


# --- fault 1: a remembered moment ------------------------------------------------------------------
def test_the_text_of_a_remembered_moment_reaches_the_checker_and_the_writer(cloister, monkeypatch):
    """Break caught (6af; failed before 6ah): "do you remember which part got to me?" shares no
    word with the monk's passage, so only what was read last is fetched, and "the monk being
    sent away from Athos wrecked you" is cut for want of the text it stands on."""
    api, app, _settings, _bid = cloister
    remember(app, MOMENT)                                            # said on the home screen
    seen, _ = fresh_ask(app, monkeypatch, api, WHICH)
    checked, prompt = read_given(seen)
    assert seen["checked"] and all(any(ATHOS in p for p in passages) for passages in checked)
    assert ATHOS in prompt.split("WHAT YOU HAVE READ TOGETHER")[1].split(BLOCK_HEAD)[0]
    # The writer has exactly the checker's passages, in the same order.
    evidence = [p for p in seen["checked"][-1]["passages"] if p.startswith("From “")]
    assert all(f"[{n}] {p}" in prompt for n, p in enumerate(evidence, 1))
    # The moment's own text is ranked first among the read text, and nothing is given twice.
    assert ATHOS in evidence[0] and len(evidence) == len(set(evidence))


def test_a_note_tied_to_its_book_brings_that_books_text(cloister, monkeypatch):
    """Break caught: only a feeling said on the home screen is looked for; one said inside the
    book (tied to it by id) is not. Here it is chosen because it shares "wrecked" with the message."""
    api, app, _settings, bid = cloister
    remember(app, MOMENT, book_id=bid)
    seen, _ = fresh_ask(app, monkeypatch, api, "what wrecked me again?")
    checked, prompt = read_given(seen)
    assert all(any(ATHOS in p for p in passages) for passages in checked) and ATHOS in prompt


def test_nothing_is_added_when_no_read_text_matches_the_note(cloister, monkeypatch):
    """A guard (passes before and after 6ah). Break caught: the search for a note falling back to
    what was read last, or adding anything else, when the note's words are nowhere in the read
    text: the passages are those of a reply without it."""
    api, app, _settings, _bid = cloister
    without, _ = fresh_ask(app, monkeypatch, api, WHICH)
    remember(app, "The storm at sea frightened the reader.")
    seen, _ = fresh_ask(app, monkeypatch, api, WHICH)
    assert BLOCK_HEAD in seen["reply"][-1]                           # the note was given
    assert read_given(seen)[0] == read_given(without)[0]
    assert not any(ATHOS in p for p in read_given(seen)[0][-1])


def test_a_note_about_text_no_longer_read_is_not_chosen_and_brings_nothing(cloister, monkeypatch):
    """A guard (passes before and after 6ah). Break caught: after chapter 1 is marked unread, the
    note, or the text it speaks of, still reaching a model. The note is not chosen (``usable``),
    and the chapter is not read text."""
    api, app, _settings, bid = cloister
    remember(app, MOMENT)
    assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1], "read": False}).status_code == 200
    seen, _ = fresh_ask(app, monkeypatch, api, WHICH)
    everything = json.dumps(seen["reply"] + seen["checked"], ensure_ascii=False)
    assert "Athos" not in everything and BLOCK_HEAD not in seen["reply"][-1]


def test_untied_notes_bring_no_text(cloister, monkeypatch):
    """A guard (passes before and after 6ah). Break caught: a note about the reader alone (a
    taste) sending the search into the books: only a note tied to a book speaks of a moment."""
    api, app, _settings, _bid = cloister
    without, _ = fresh_ask(app, monkeypatch, api, WHICH)
    remember(app, "The reader likes a monk who is sent away from Athos.", kind="taste", tied=False)
    seen, _ = fresh_ask(app, monkeypatch, api, WHICH)
    assert BLOCK_HEAD in seen["reply"][-1] and read_given(seen)[0] == read_given(without)[0]


def test_the_moments_text_counts_against_the_same_room_and_survives_fitting(cloister, monkeypatch):
    """Breaks caught: the moment's passages added on top of the room, so the largest check goes
    past the checker's limit; or ranked last, so they are the first to go when room is short."""
    api, app, _settings, _bid = cloister
    remember(app, MOMENT)
    seen, _ = fresh_ask(app, monkeypatch, api, WHICH)
    assert largest_check(seen) <= stream_conversation.check_room(WHICH, identities=())
    passages = seen["checked"][-1]["passages"]
    fixed = [p for p in passages if not p.startswith("From “")]
    moment = next(p for p in passages if ATHOS in p)

    def size(items):
        return sum(len(json.dumps(p, ensure_ascii=False).encode()) + 2 for p in items)

    overhead = stream_conversation.CHECK_INPUT_LIMIT - stream_conversation.check_room(WHICH, identities=())
    monkeypatch.setattr(stream_conversation, "CHECK_INPUT_LIMIT", overhead + size(fixed + [moment]) + 10)
    seen, _ = fresh_ask(app, monkeypatch, api, WHICH)
    assert largest_check(seen) <= stream_conversation.check_room(WHICH, identities=())
    assert all([p for p in state["passages"] if p.startswith("From “")] == [moment] for state in seen["checked"])
    assert notes_given(seen)


def notes_given(seen):
    return [p for state in seen["checked"] for p in state["passages"] if p.startswith(PASSAGE_HEAD)]


def test_at_most_two_passages_a_note_three_notes_and_only_the_books_they_name(monkeypatch):
    """Breaks caught: a long list of notes, or a note whose words are everywhere, filling the
    room with their passages before the message's own; a note tied to one book searching
    another; a passage given twice; a note not tied to a book searched at all."""
    asked = []

    def found(item, text, *, limit):
        asked.append((item["book"]["book_id"], text))
        return [f"From “{item['book']['book_id']}”: {text} {n}" for n in range(limit + 3)] + ["shared"]

    monkeypatch.setattr(home_conversation, "read_passages", found)
    read = [{"book": {"book_id": "a"}}, {"book": {"book_id": "b"}}]

    def note(number, book_id=None, tied=True):
        return ({"text": f"note {number}", "tied": tied, "book_id": book_id, "kind": "feeling"}, "-")

    chosen = [note(0, tied=False), note(1, "a"), note(2), note(3, "b"), note(4, "a"), note(5)]
    given = home_conversation.moment_passages(chosen, read)
    assert ("a", "note 0") not in asked and ("b", "note 0") not in asked      # untied: not searched
    assert [text for _, text in asked] == ["note 1", "note 2", "note 2", "note 3"]   # three notes only
    assert ("b", "note 1") not in asked and ("a", "note 3") not in asked    # only the book named
    assert given == ["From “a”: note 1 0", "From “a”: note 1 1",           # two a note, books in turn
                     "From “a”: note 2 0", "From “b”: note 2 0",
                     "From “b”: note 3 0", "From “b”: note 3 1"]
    assert home_conversation.moment_passages([note(1, "c")], read) == []    # its book has no read text
    monkeypatch.setattr(home_conversation, "read_passages", lambda item, text, *, limit: ["same"])
    assert home_conversation.moment_passages([note(1), note(2)], read) == ["same"]    # never twice


# --- fault 2: a book the reader has named ----------------------------------------------------------
def test_a_book_named_in_the_message_heads_the_list_the_writer_and_checker_are_given(shelf, monkeypatch):
    """Break caught (6af; failed before 6ah): "maybe jane eyre", and Jane Eyre is not among the
    six, so Mothli says it does not know enough about it."""
    api, app, _settings = shelf
    seen = models(app, monkeypatch, reply="You might like Jane Eyre by Charlotte Brontë.")
    ask(api, "any ideas? something lighter, maybe jane eyre")
    lines = block(seen["reply"][-1])
    assert lines[0].startswith("- “Jane Eyre” by Charlotte Bront") and len(lines) == 6
    passages = seen["checked"][-1]["passages"]
    assert sum(p.endswith(lines[0][2:]) for p in passages) == 1


def test_a_book_named_only_in_a_chosen_plan_heads_the_list(shelf, monkeypatch):
    """Break caught (6af; failed before 6ah): the reader said "maybe jane eyre" earlier, it was
    kept as a plan, and asked later for a suggestion Mothli has no line for it. With the setting
    off, the plan is not read and the list is as it was."""
    api, app, settings = shelf
    settings.reader_recollections = True
    app.state.catalog.keep_recollections([{
        "kind": "plan", "text": "The reader wants something lighter after this, perhaps Jane Eyre.", "tied": False,
        "book_id": None, "drew_on": None, "said": "x", "said_at": "2026-11-16T09:00:00+00:00", "replaces": None}],
        cap=recollections.STORE_CAP)
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, "Can you suggest something new to read?")
    assert block(seen["reply"][-1])[0].startswith("- “Jane Eyre”")
    settings.reader_recollections = False
    app.state.catalog.clear_turns(None)
    ask(api, "Can you suggest something new to read?")
    assert not any("Jane Eyre" in line for line in block(seen["reply"][-1]))


def test_a_book_named_in_a_note_that_is_not_a_plan_or_in_mothlis_words_does_not_move(shelf, monkeypatch):
    """A guard (passes before and after 6ah). Break caught: names taken from anything but the
    reader's message and their plans: a taste note, or Mothli's own earlier reply."""
    api, app, settings = shelf
    settings.reader_recollections = True
    app.state.catalog.keep_recollections([{
        "kind": "taste", "text": "The reader loved Jane Eyre at school.", "tied": False, "book_id": None,
        "drew_on": None, "said": "x", "said_at": "2026-11-16T09:00:00+00:00", "replaces": None}],
        cap=recollections.STORE_CAP)
    catalog = app.state.catalog
    where = dict(incarnation=HOME, position_epoch=0, atom=0, offset=0, drew_on={})
    catalog.add_turn(None, role="user", content="hello", **where)
    catalog.add_turn(None, role="assistant", content="You might like Jane Eyre.", **where)
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, "Can you suggest something new to read?")
    assert not any("Jane Eyre" in line for line in block(seen["reply"][-1]))
    assert BLOCK_HEAD in seen["reply"][-1]                           # the note itself was given


def test_a_named_book_on_the_shelf_is_not_listed_and_the_rest_is_as_before(shelf, monkeypatch):
    """A guard (passes before and after 6ah). Break caught: naming a shelf book ("Middlemarch",
    not started) puts it on the list of books not on the shelf; and with nothing named the list
    is not the one main gives."""
    api, app, _settings = shelf
    seen = models(app, monkeypatch, reply="Hello.")
    ask(api, "Can you suggest something new to read?")
    plain = block(seen["reply"][-1])
    ask(api, "Can you suggest something new to read, like middlemarch?")
    assert block(seen["reply"][-1]) == plain and not any("Middlemarch" in line for line in plain)
    assert [line.split("“")[1].split("”")[0] for line in plain] == [
        "Pride and Prejudice", "Frankenstein", "Moby Dick", "Alice's Adventures in Wonderland",
        "The Adventures of Sherlock Holmes", "Dracula"]


# --- fault 3: "what's an elder?" after five earlier messages ----------------------------------------
EARLIER = ["honestly i love how dry the narrator is. long descriptions bore me though",
           "keep your answers short pls, i mostly read on the train at night",
           "that bit about the monk sent away from athos wrecked me",
           "who is Alyosha again?",
           "i think after this one i want something lighter, maybe jane eyre"]
ELDER = "An elder was one who took your soul, your will, into his soul and his will."


@pytest.mark.parametrize("remembering", [False, True])
def test_what_an_elder_is_reaches_the_home_reply_after_earlier_messages(tmp_path, monkeypatch, remembering):
    """Break caught (6af; failed before 6ah): the search words are the message and the last two
    messages of the conversation, and "maybe jane eyre" pushed the one passage that says what an
    elder is out of the home chat's ten, so a reply said "we haven't yet read an explanation".
    The Karamazov fixture at ``inside-5``, the free library on, the five earlier messages of that
    run, with and without what the reader said kept as notes."""
    from scripts.companion_eval import enter_by_map, karamazov_epub
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    settings.classics_enabled, settings.reader_recollections = True, remembering
    cases = json.loads((SCRIPTS / "companion_eval_cases.json").read_text(encoding="utf-8"))
    inside = next(p for p in cases["books"]["karamazov"]["positions"] if p["name"] == "inside-5")
    with TestClient(app) as api:
        bid = api.post("/api/books", files={"file": ("k.epub", karamazov_epub(), "application/epub+zip")}).json()["book_id"]
        _boundary, read_text, _later = enter_by_map(api, app, bid, inside, app.state.worker.source_chapters(bid))
        assert ELDER in " ".join(read_text.split())                        # read, in chapter 5
        catalog = app.state.catalog
        drew_on = {bid: read_now(app)[bid]}
        where = dict(incarnation=HOME, position_epoch=0, atom=0, offset=0, drew_on=drew_on)
        for message in EARLIER:
            catalog.add_turn(None, role="user", content=message, **where)
            catalog.add_turn(None, role="assistant", content="Got it.", **where)
        if remembering:
            catalog.keep_recollections([
                {"kind": kind, "text": text, "tied": tied, "book_id": None, "drew_on": drew_on if tied else None,
                 "said": "x", "said_at": "2026-11-16T09:00:00+00:00", "replaces": None}
                for kind, text, tied in [
                    ("taste", "The reader loves the narrator's dry tone and is bored by long descriptions.", False),
                    ("about", "The reader wants short answers and reads on the train at night.", False),
                    ("feeling", MOMENT, True),
                    ("plan", "The reader wants something lighter after this, perhaps Jane Eyre.", False)]],
                cap=recollections.STORE_CAP)
        seen = models(app, monkeypatch, reply="An elder takes your soul and your will into his own.")
        ask(api, "what's an elder?")
    assert seen["checked"] and all(any(ELDER in p for p in state["passages"]) for state in seen["checked"])
    assert ELDER in seen["reply"][-1].split("WHAT YOU HAVE READ TOGETHER")[1]
    assert largest_check(seen) <= stream_conversation.check_room("what's an elder?", identities=())
