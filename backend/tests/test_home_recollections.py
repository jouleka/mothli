"""What Mothli remembers about the reader, in the home chat (part two of "recollections").

The home chat is given exactly what the chat inside a book is given: the same block for the
writer, the same passage for the sentence checks, the same one instruction, all only for a reply
that has notes. Its messages are distilled the same way, and clearing it keeps the notes. Stand-in
writer and Jev throughout; each test names the break it catches.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_home_conversation import ask, book_ids, lamp, read_fresh, shelf as shelf  # noqa: E402,F401
from test_recollections import NIGHT, change, chatter, idle, kept, models  # noqa: E402

from app import recollections, stream_conversation  # noqa: E402
from app.home_conversation import HOME, HOME_SYSTEM  # noqa: E402
from app.reader_place import SPEAK_AS_A_FRIEND  # noqa: E402
from app.recollections import BLOCK_HEAD, PASSAGE_HEAD, RECOLLECTION_RULE  # noqa: E402

REPLY = "Corvus moved lamp 21 in part 2."
SHORT = "The reader wants short answers."
FELT = "The reader was moved by the lamps."


def remember(app, text, *, kind="about", book=None):
    """Store one note as distilling would. ``book``: the id of the book it is tied to, with what
    is read of that book now as the record of what was read when it was said."""
    catalog = app.state.catalog
    drew_on = None
    if book is not None:
        incarnation = catalog.get_book(book)["incarnation"]
        drew_on = {book: [incarnation, catalog.get_read_map(book, incarnation=incarnation)]}
    catalog.keep_recollections([{"kind": kind, "text": text, "tied": book is not None, "book_id": book,
                                 "drew_on": drew_on, "said": "x", "said_at": "2026-11-16T09:00:00+00:00",
                                 "replaces": None}], cap=recollections.STORE_CAP)


def home_said(app, *messages, reply="Mm."):
    """Store reader messages in the home conversation as the home chat does, without a reply."""
    catalog = app.state.catalog
    drew_on = {item["book_id"]: [item["incarnation"],
                                 catalog.get_read_map(item["book_id"], incarnation=item["incarnation"])]
               for item in catalog.list_books()}
    where = dict(incarnation=HOME, position_epoch=0, atom=0, offset=0,
                 drew_on={book: record for book, record in drew_on.items() if record[1]})
    for message in messages:
        catalog.add_turn(None, role="user", content=message, **where)
        catalog.add_turn(None, role="assistant", content=reply, **where)


def block_of(prompt):
    if BLOCK_HEAD not in prompt:
        return None
    return BLOCK_HEAD + prompt.split(BLOCK_HEAD, 1)[1].split("\n\n", 1)[0]


def notes_passages(seen):
    return [p for state in seen["checked"] for p in state["passages"] if p.startswith(PASSAGE_HEAD)]


def largest_check(seen):
    return max(sum(len(json.dumps(p, ensure_ascii=False).encode()) + 2 for p in state["passages"])
               for state in seen["checked"])


def fresh_ask(app, monkeypatch, api, question):
    """One home reply from an empty conversation, so that two replies can be compared byte for byte."""
    app.state.catalog.clear_turns(None)
    seen = models(app, monkeypatch, reply=REPLY)
    events = ask(api, question)
    return seen, events


# --- unchanged without notes ------------------------------------------------------------------------
def test_the_home_prompt_and_system_are_unchanged_with_no_notes_or_with_the_setting_off(shelf, monkeypatch):
    """The guard written first: it passed before the change and must pass after it. Breaks
    caught: switching the feature on changes every home reply's instructions or prompt before a
    single note exists; or notes are used with the setting off."""
    api, app, settings = shelf
    settings.reader_recollections = False
    before, _ = fresh_ask(app, monkeypatch, api, "What did Corvus do with lamp 21?")
    settings.reader_recollections = True
    empty, _ = fresh_ask(app, monkeypatch, api, "What did Corvus do with lamp 21?")
    remember(app, SHORT)
    settings.reader_recollections = False
    off, _ = fresh_ask(app, monkeypatch, api, "What did Corvus do with lamp 21?")
    for seen in (before, empty, off):
        assert seen["system"][-1] == HOME_SYSTEM and seen["reply"][-1] == before["reply"][-1]
        assert BLOCK_HEAD not in seen["reply"][-1] and notes_passages(seen) == []
        assert [s["passages"] for s in seen["checked"]] == [s["passages"] for s in before["checked"]]


# --- used in the home chat --------------------------------------------------------------------------
def test_notes_reach_the_home_writer_and_the_home_checker(shelf, monkeypatch):
    """Breaks caught (each failed before this change): the home chat is never given the notes;
    the writer is given them and the checker is not, so "you told me…" is cut; the instruction
    is missing; a note tied to a book on the shelf is never used at home."""
    api, app, settings = shelf
    settings.reader_recollections = True
    lamps = book_ids(app)["The Lamp Book"]
    remember(app, FELT, kind="feeling", book=lamps)
    remember(app, SHORT)
    seen, events = fresh_ask(app, monkeypatch, api, "What did Corvus do with the lamps?")
    prompt, system = seen["reply"][-1], seen["system"][-1]
    assert block_of(prompt) == (BLOCK_HEAD + "\n- " + SHORT + "\n- About “The Lamp Book”: " + FELT)
    assert prompt.endswith(block_of(prompt) + "\n\nReply to the reader now.")
    assert system == HOME_SYSTEM.replace(SPEAK_AS_A_FRIEND, RECOLLECTION_RULE + SPEAK_AS_A_FRIEND)
    assert seen["checked"] and all(
        [p for p in state["passages"] if p.startswith(PASSAGE_HEAD)]
        == [PASSAGE_HEAD + SHORT + " About “The Lamp Book”: " + FELT] for state in seen["checked"])
    assert [e["text"] for e in events if e["type"] == "sentence"] == [REPLY]


def test_a_note_tied_to_a_book_leaves_the_home_chat_when_that_book_is_read_fresh_and_comes_back(shelf, monkeypatch):
    """Break caught (failed before this change, as nothing was given): after "read it fresh" on
    one book, the home chat still says what the reader felt about pages it is meant to have
    forgotten; or it never brings the note back once they are read again."""
    api, app, settings = shelf
    settings.reader_recollections = True
    lamps = book_ids(app)["The Lamp Book"]
    remember(app, FELT, kind="feeling", book=lamps)
    remember(app, SHORT)

    def given():
        seen, _ = fresh_ask(app, monkeypatch, api, "What about the lamps?")
        return json.dumps(seen["reply"] + seen["checked"], ensure_ascii=False)

    assert FELT in given() and SHORT in given()
    read_fresh(api, lamps)
    everything = given()
    assert FELT not in everything and SHORT in everything      # the note that says nothing of a book stays
    mark = {"cfi": "epubcfi(/6/2)", "position_epoch": 1, "atom": 2}
    assert api.put(f"/api/books/{lamps}/read-page", json={
        "start": {**mark, "quote": lamp(2, 20), "prefix": ""},
        "end": {**mark, "quote": lamp(2, 24), "prefix": lamp(2, 24)}}).status_code == 200
    assert FELT in given()


def test_a_fault_in_reading_the_notes_costs_the_home_reader_the_notes_not_the_reply(shelf, monkeypatch):
    """Break caught: a damaged row in the store, and the home chat answers nothing."""
    api, app, settings = shelf
    settings.reader_recollections = True
    monkeypatch.setattr("app.home_conversation.recall", lambda *args, **more: 1 / 0)
    seen, events = fresh_ask(app, monkeypatch, api, "What did Corvus do with lamp 21?")
    assert [e["text"] for e in events if e["type"] == "sentence"] == [REPLY]
    assert seen["system"][-1] == HOME_SYSTEM and BLOCK_HEAD not in seen["reply"][-1]


# --- distilled -------------------------------------------------------------------------------------
def test_a_home_message_is_distilled_once(shelf, monkeypatch):
    """Breaks caught (failed before this change): nothing ever starts the distilling from the
    home chat, so what the reader says there is never remembered; or it is read twice."""
    api, app, settings = shelf
    settings.reader_recollections = True
    seen = models(app, monkeypatch, reply=REPLY, changes=lambda prompt: [
        change("The reader reads at night, on the train home.", message=6)] if NIGHT in prompt else [])
    home_said(app, *chatter(5))
    ask(api, NIGHT)                                                  # the sixth message, through the app
    idle(app)
    assert kept(app) == [("about", "The reader reads at night, on the train home.")]
    assert len(seen["distil"]) == 1 and "(written on the home screen)" in seen["distil"][0]
    [note] = app.state.catalog.get_recollections()
    assert note["said"] == NIGHT and not note["tied"]
    ask(api, "And lamp 22?")
    idle(app)
    assert len(seen["distil"]) == 1                                  # one new message: nothing asked

    settings.reader_recollections = False
    home_said(app, *chatter(8, start=10))
    ask(api, "One more?")
    idle(app)
    assert len(seen["distil"]) == 1                                  # and nothing with the setting off


def test_clearing_the_home_conversation_distils_first_and_keeps_the_notes(shelf, monkeypatch):
    """Breaks caught (failed before this change): "Clear conversation" on the home screen throws
    away what the reader said before anyone looked at it; or takes the notes with it."""
    api, app, settings = shelf
    settings.reader_recollections = True
    seen = models(app, monkeypatch, changes=lambda prompt: [change("The reader reads at night.")]
                  if NIGHT in prompt else [])
    remember(app, SHORT)
    home_said(app, *chatter(6))
    app.state.recollector.catch_up(None)                             # these six have been looked at
    assert len(seen["distil"]) == 1
    home_said(app, NIGHT, "And lamp 2?")                              # two more: below the batch size
    assert api.delete("/api/reader/conversation").status_code == 204
    assert api.get("/api/reader/conversation").json()["turns"] == []
    idle(app)
    assert len(seen["distil"]) == 2 and NIGHT in seen["distil"][1] and "lamp number 3" not in seen["distil"][1]
    assert sorted(kept(app)) == [("about", "The reader reads at night."), ("about", SHORT)]


# --- inside the checker's size limit ---------------------------------------------------------------
def test_with_a_full_shelf_read_text_gives_way_to_the_notes_and_the_largest_check_stays_inside_the_limit(
        shelf, monkeypatch):
    """Breaks caught (the first two failed before this change): the notes pushed out by read
    text, or added on top of it so the largest check goes over the limit and is refused (6ad);
    with no room even for the fixed passages, the writer given notes its checker is not."""
    api, app, settings = shelf
    settings.reader_recollections = True
    ids = book_ids(app)
    for bid in ids.values():                                         # every book read in full
        assert api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1, 2, 3], "read": True}).status_code == 200
    remember(app, SHORT)
    remember(app, FELT, kind="feeling", book=ids["The Lamp Book"])
    question = "What did Corvus do with the lamps and Jonas with the carts?"
    seen, _ = fresh_ask(app, monkeypatch, api, question)
    room = stream_conversation.check_room(question, identities=())
    assert largest_check(seen) <= room
    passages = seen["checked"][-1]["passages"]
    read = [p for p in passages if p.startswith("From “")]
    fixed = [p for p in passages if not p.startswith("From “")]
    assert len(read) >= 2 and fixed[-1].startswith(PASSAGE_HEAD)

    def size(items):
        return sum(len(json.dumps(p, ensure_ascii=False).encode()) + 2 for p in items)

    # Room for the fixed passages, the notes among them, and not one read passage: the read text goes.
    overhead = stream_conversation.CHECK_INPUT_LIMIT - room
    monkeypatch.setattr(stream_conversation, "CHECK_INPUT_LIMIT", overhead + size(fixed) + 10)
    seen, _ = fresh_ask(app, monkeypatch, api, question)
    assert largest_check(seen) <= stream_conversation.check_room(question, identities=())
    assert notes_passages(seen) and block_of(seen["reply"][-1])
    assert not any(p.startswith("From “") for state in seen["checked"] for p in state["passages"])

    # Room for the fixed passages without the notes: writer and checker both go without them.
    monkeypatch.setattr(stream_conversation, "CHECK_INPUT_LIMIT", overhead + size(fixed[:-1]) + 10)
    seen, _ = fresh_ask(app, monkeypatch, api, question)
    assert notes_passages(seen) == [] and block_of(seen["reply"][-1]) is None
    assert seen["system"][-1] == HOME_SYSTEM
    assert largest_check(seen) <= stream_conversation.check_room(question, identities=())
