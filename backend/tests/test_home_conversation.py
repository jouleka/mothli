"""Mothli on the home screen: one conversation across the reader's books. For every book it has
only what the reader has read of that book; for a book not started, only that it is on the shelf."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import epub_ncx  # noqa: E402
from test_chapter_notes import models  # noqa: E402
from test_ingest import InlineExecutor  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

LAMP = {1: "Mara", 2: "Corvus", 3: "Vex"}
CART = {1: "Jonas", 2: "Petra", 3: "Ilse"}


def lamp(chapter, number):
    return f"{LAMP[chapter]} moved lamp {number} in part {chapter}."


def cart(chapter, number):
    return f"{CART[chapter]} pushed cart {number} in section {chapter}."


def book(api, title, author, sentence):
    epub = epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}", " ".join(sentence(n, i) for i in range(1, 41)))
                     for n in (1, 2, 3)], title=title, author=author)
    return api.post("/api/books", files={"file": (title + ".epub", epub, "application/epub+zip")}).json()["book_id"]


@pytest.fixture
def shelf(tmp_path):
    """Three books: a page read in the middle of one, the first chapter of another, one not started."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    with TestClient(app) as api:
        lamps = book(api, "The Lamp Book", "Ada Writer", lamp)
        carts = book(api, "The Cart Book", "Ben Author", cart)
        unread = book(api, "Middlemarch", "George Eliot", lambda n, i: f"UNREAD_SHELF_CANARY {n} {i}.")
        for bid in (lamps, carts, unread):
            api.get(f"/api/books/{bid}/read-frontier")                 # opened once
        mark = {"cfi": "epubcfi(/6/2)", "position_epoch": 0, "atom": 2}
        assert api.put(f"/api/books/{lamps}/read-page", json={
            "start": {**mark, "quote": lamp(2, 20), "prefix": ""},
            "end": {**mark, "quote": lamp(2, 24), "prefix": lamp(2, 24)}}).status_code == 200
        assert api.put(f"/api/books/{carts}/read-chapters", json={"atoms": [1], "read": True}).status_code == 200
        yield api, app, settings


def ask(api, question):
    response = api.post("/api/reader/ask/stream", json={"question": question})
    assert response.status_code == 200, response.text
    return [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]


def test_a_home_reply_has_each_books_read_text_under_its_title_and_nothing_unread(shelf, monkeypatch):
    api, app, _ = shelf
    seen = models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2. She later sold it abroad.",
                  unsupported=["abroad"])
    events = ask(api, "What did Corvus do with lamp 21, and what did Jonas do with cart 7?")
    prompt = seen["reply"][-1]
    assert lamp(2, 21) in prompt and cart(1, 7) in prompt
    assert "From “The Lamp Book”" in prompt and "From “The Cart Book”" in prompt
    # Nothing outside what was read: the rest of either book, or any of the one not started.
    assert lamp(2, 19) not in prompt and "Mara" not in prompt and "Petra" not in prompt
    assert "UNREAD_SHELF_CANARY" not in prompt
    assert "“Middlemarch” by George Eliot: not started" in prompt
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Corvus moved lamp 21 in part 2."]
    assert events[-1]["type"] == "done" and events[-1]["answer"]["dropped_sentences"] == 1
    # The checker saw what the writer saw, each passage under its book's name.
    passages = " ".join(seen["checked"][-1]["passages"])
    assert "From “The Lamp Book”" in passages and "UNREAD_SHELF_CANARY" not in passages and "Petra" not in passages


def test_the_shelf_tells_mothli_where_the_reader_is_in_each_book(shelf, monkeypatch):
    api, app, _ = shelf
    api.post("/api/history/import", json={"source": "goodreads", "books": [
        {"title": "Deep Work", "author": "Cal Newport", "status": "read", "rating": 4, "finished_on": "2022-02-01"}]})
    seen = models(app, monkeypatch, reply="You’re one chapter into The Cart Book.")
    ask(api, "Where am I in each of my books?")
    prompt = seen["reply"][-1]
    # In the reader's terms: where they are, and what was not read with Mothli. Never a count
    # of "finished" chapters, which told a reader in chapter 2 that they had read nothing.
    assert "“The Cart Book” by Ben Author:" in prompt and "Read with you: chapter 1 in full." in prompt
    assert ("“The Lamp Book” by Ada Writer: the reader is in chapter 2 of 3 now. Read with you: part of "
            "chapter 2. Not read with you: chapter 1, which come before where the reader is.") in prompt
    assert "finished" not in prompt.split("WHAT YOU HAVE READ TOGETHER")[0].split("WHERE THE READER IS")[1].split(
        "Deep Work")[0]
    assert "“Deep Work” by Cal Newport: read in another app, finished 2022-02-01, rated 4" in prompt
    assert "HOW THIS APP WORKS" in prompt and "open Contents" in prompt
    assert any("“The Cart Book” by Ben Author" in " ".join(s["passages"]) for s in seen["checked"])


def test_the_home_conversation_is_kept_and_can_be_cleared(shelf, monkeypatch):
    api, app, _ = shelf
    seen = models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2. She later sold it abroad.",
                  unsupported=["abroad"])
    ask(api, "What did Corvus do with lamp 21?")
    turns = lambda: [(t["role"], t["content"]) for t in api.get("/api/reader/conversation").json()["turns"]]  # noqa: E731
    assert turns() == [("user", "What did Corvus do with lamp 21?"), ("assistant", "Corvus moved lamp 21 in part 2.")]
    ask(api, "And then?")
    assert "What did Corvus do with lamp 21?" in seen["reply"][-1] and "abroad" not in seen["reply"][-1]
    assert api.delete("/api/reader/conversation").status_code == 204
    assert turns() == []


def test_an_outage_shows_nothing_and_keeps_nothing(shelf, monkeypatch):
    api, app, _ = shelf
    models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2.")

    def down(state, questions):
        from app.llm.jev import JevUnavailable
        raise JevUnavailable("down")
    app.state.client.jev.evaluate = down
    response = api.post("/api/reader/ask/stream", json={"question": "What did Corvus do?"})
    events = [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]
    assert [e["type"] for e in events] == ["error"] and "lamp" not in response.text
    assert api.get("/api/reader/conversation").json()["turns"] == []


def test_a_refused_home_check_tells_the_owner_why_and_charges_no_book(shelf, monkeypatch, caplog):
    """The home chat hid the provider's reason too, and charged its refused checks to a book."""
    from test_jev import NO_CREDITS, provider
    api, app, _ = shelf
    models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2.")
    app.state.client.jev = provider(402)
    with caplog.at_level("WARNING", logger="mothli.conversation"):
        response = api.post("/api/reader/ask/stream", json={"question": "What did Corvus do?"})
    events = [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]
    assert events == [{"type": "error", "status": 503, "reason": "no_credits",
                       "message": "Jev review unavailable; no unchecked reply was published"}]
    assert "lamp" not in response.text and "organization" not in response.text
    [record] = [json.loads(r.getMessage()) for r in caplog.records
                if r.name == "mothli.conversation" and r.levelname == "WARNING"]
    assert record["event"] == "conversation-home" and record["failure"] == "JevUnavailable"
    assert record["reason"] == "no_credits" and record["provider_message"] == NO_CREDITS
    assert "private-test-key" not in caplog.text + response.text
    catalog = app.state.catalog
    for item in catalog.list_books():
        assert not [c for c in catalog.get_costs(item["book_id"]) if c["model"].startswith("jev")]
        assert catalog.get_cost_reservations(item["book_id"]) == []
    assert api.get("/api/reader/conversation").json()["turns"] == []


def test_home_chat_needs_the_read_map_and_an_empty_shelf_is_answered(shelf, monkeypatch, tmp_path):
    """An empty shelf used to get no model call and the fixed "nothing passed" line (6al); it
    now gets a checked reply: see test_first_hello.py."""
    api, app, settings = shelf
    settings.reader_read_map = False
    assert api.post("/api/reader/ask/stream", json={"question": "Hello there"}).status_code == 404
    bare = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                    reader_conversation_pipeline=True, data_dir=str(tmp_path / "empty"))
    empty = create_app(bare, ingest_executor=InlineExecutor())
    bare.reader_conversation_jev = bare.reader_conversation_stream = bare.reader_read_map = True
    with TestClient(empty) as other:
        seen = models(empty, monkeypatch, reply="Hello.")
        events = ask(other, "What should I read?")
        assert [e["type"] for e in events] == ["sentence", "done"] and not events[-1]["answer"]["insufficient_evidence"]
        assert len(seen["reply"]) == 1 and len(seen["checked"]) == 1


def test_a_home_reply_that_says_mothli_was_not_there_offers_to_count_those_chapters(shelf, monkeypatch):
    api, app, _ = shelf
    models(app, monkeypatch, reply="I wasn’t with you for chapter 1 of The Lamp Book.")
    offers = ask(api, "why dont you know the start of the lamp book")[-1]["answer"]["offers"]
    assert [(o["title"], o["atoms"], o["chapters"]) for o in offers] == [("The Lamp Book", [1], "chapter 1")]
    models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2.")
    assert "offers" not in ask(api, "What did Corvus do?")[-1]["answer"]


# --- earlier home turns, after what is read of a book shrinks ------------------------------------
OLD = ("What did Corvus and Jonas each do?", "Corvus moved lamp 21 in part 2. Jonas pushed cart 7 in section 1.")


def book_ids(app):
    return {item["title"]: item["book_id"] for item in app.state.catalog.list_books()}


def read_fresh(api, bid, epoch=0):
    response = api.post(f"/api/books/{bid}/position/reset", json={"position_epoch": epoch, "fresh": True})
    assert response.status_code == 200, response.text


def shown(api):
    return [turn["content"] for turn in api.get("/api/reader/conversation").json()["turns"]]


def said_before(app, monkeypatch, api):
    """One home exchange that draws on both books with read text."""
    models(app, monkeypatch, reply=OLD[1])
    ask(api, OLD[0])
    assert shown(api) == list(OLD)


def test_a_home_turn_is_not_said_or_shown_again_once_its_book_is_read_fresh(shelf, monkeypatch):
    """Catches: after "read it fresh", an old home turn about that book still handed to the writer
    as conversation, to the checker as the reader's message, and to the screen."""
    api, app, _ = shelf
    said_before(app, monkeypatch, api)
    read_fresh(api, book_ids(app)["The Lamp Book"])
    assert shown(api) == []
    seen = models(app, monkeypatch, reply="Jonas pushed cart 9 in section 1.")
    ask(api, "And then?")
    prompt = seen["reply"][-1]
    assert "Corvus" not in prompt and OLD[0] not in prompt and "lamp 21" not in prompt
    assert "Corvus" not in json.dumps(seen["checked"])
    assert "“The Lamp Book” by Ada Writer: not started" in prompt
    assert shown(api) == ["And then?", "Jonas pushed cart 9 in section 1."]


def test_a_home_turn_goes_when_only_one_of_the_books_it_drew_on_is_read_fresh(shelf, monkeypatch):
    """Catches: a turn kept because one of its books is still read, or because the check looked
    at one book only. The lamp book is untouched; the turn also holds the cart book."""
    api, app, _ = shelf
    said_before(app, monkeypatch, api)
    read_fresh(api, book_ids(app)["The Cart Book"])
    assert shown(api) == []
    seen = models(app, monkeypatch, reply="Corvus moved lamp 22 in part 2.")
    ask(api, "And then?")
    assert "Jonas" not in seen["reply"][-1] and OLD[0] not in seen["reply"][-1]
    assert "Jonas" not in json.dumps(seen["checked"])


def test_a_home_turn_stays_while_its_books_are_only_read_further_or_read_again(shelf, monkeypatch):
    """Catches: hiding too much. More pages, another chapter, and "read again" (which keeps what
    was read) all leave every book read at least as far as when the turn was written."""
    api, app, _ = shelf
    said_before(app, monkeypatch, api)
    ids = book_ids(app)
    mark = {"cfi": "epubcfi(/6/2)", "position_epoch": 0, "atom": 2}
    assert api.put(f"/api/books/{ids['The Lamp Book']}/read-page", json={
        "start": {**mark, "quote": lamp(2, 30), "prefix": ""},
        "end": {**mark, "quote": lamp(2, 33), "prefix": lamp(2, 33)}}).status_code == 200
    assert api.put(f"/api/books/{ids['The Cart Book']}/read-chapters",
                   json={"atoms": [2], "read": True}).status_code == 200
    again = api.post(f"/api/books/{ids['The Cart Book']}/position/reset", json={"position_epoch": 0})
    assert again.status_code == 200, again.text
    assert shown(api) == list(OLD)
    seen = models(app, monkeypatch, reply="Petra pushed cart 3 in section 2.")
    ask(api, "And then?")
    assert OLD[0] in seen["reply"][-1]
    assert shown(api) == [*OLD, "And then?", "Petra pushed cart 3 in section 2."]


def test_a_home_turn_written_when_nothing_was_read_outlives_a_reset(tmp_path, monkeypatch):
    """Catches: a turn that drew on no book (the app, a greeting) being taken for one whose books
    cannot be known, and hidden with those. It holds nothing of any book, so no reset touches it."""
    settings = Settings(_env_file=None, allow_stub=True, reader_page_context=True,
                        reader_conversation_pipeline=True, data_dir=str(tmp_path / "data"))
    app = create_app(settings, ingest_executor=InlineExecutor())
    settings.reader_conversation_jev = settings.reader_conversation_stream = settings.reader_read_map = True
    hello = ("How do I tell you I have read a chapter?", "Open Contents and press the label beside the chapter.")
    with TestClient(app) as api:
        lamps = book(api, "The Lamp Book", "Ada Writer", lamp)
        api.get(f"/api/books/{lamps}/read-frontier")
        models(app, monkeypatch, reply=hello[1])
        ask(api, hello[0])
        assert api.put(f"/api/books/{lamps}/read-chapters", json={"atoms": [1], "read": True}).status_code == 200
        models(app, monkeypatch, reply="Mara moved lamp 4 in part 1.")
        ask(api, "What did Mara do with lamp 4?")
        assert shown(api) == [*hello, "What did Mara do with lamp 4?", "Mara moved lamp 4 in part 1."]
        read_fresh(api, lamps)
        assert shown(api) == list(hello)
        seen = models(app, monkeypatch, reply="Press it again to take it back.")
        ask(api, "And to undo it?")
        assert hello[0] in seen["reply"][-1] and "Mara" not in seen["reply"][-1]


def test_a_home_turn_stored_without_its_books_is_never_said_or_shown(shelf, monkeypatch):
    """Catches: trusting a turn stored before turns recorded their books. Nothing says what it
    drew on or how far each book was read then, so it cannot be shown safe, reset or no reset."""
    api, app, _ = shelf
    for role, content in (("user", "Why did Vex leave in part 3?"), ("assistant", "Vex moved lamp 40 in part 3.")):
        app.state.catalog.add_turn(None, incarnation="home", position_epoch=0, atom=0, offset=0,
                                   role=role, content=content)
    assert shown(api) == []
    seen = models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2.")
    ask(api, "And then?")
    assert "Vex" not in seen["reply"][-1] and "Vex" not in json.dumps(seen["checked"])
    assert shown(api) == ["And then?", "Corvus moved lamp 21 in part 2."]
    assert api.delete("/api/reader/conversation").status_code == 204          # clearing removes those too
    assert app.state.catalog.get_turns(None, incarnation="home") == []


# --- further ways an old turn could outlive the text it was written from -------------------------
def test_unmarking_one_chapter_hides_the_turn_and_reading_it_again_brings_it_back(shelf, monkeypatch):
    """Catches: a rule that looks only for a reset. "I haven't read this" on one chapter shrinks
    the map with no reset at all; marking it read again restores exactly what the turn drew on."""
    api, app, _ = shelf
    said_before(app, monkeypatch, api)
    carts = book_ids(app)["The Cart Book"]
    assert api.put(f"/api/books/{carts}/read-chapters", json={"atoms": [1], "read": False}).status_code == 200
    assert shown(api) == []
    seen = models(app, monkeypatch, reply="Corvus moved lamp 22 in part 2.")
    ask(api, "And then?")
    assert "Jonas" not in seen["reply"][-1] and OLD[0] not in seen["reply"][-1]
    assert api.put(f"/api/books/{carts}/read-chapters", json={"atoms": [1], "read": True}).status_code == 200
    assert shown(api)[:2] == list(OLD)


def test_a_turn_about_a_book_taken_off_the_shelf_is_gone_and_a_new_copy_does_not_bring_it_back(shelf, monkeypatch):
    """Catches: home turns have no book of their own, so removing a book left them behind, to be
    said about a new, unread copy of the same book."""
    api, app, _ = shelf
    said_before(app, monkeypatch, api)
    assert api.delete(f"/api/books/{book_ids(app)['The Cart Book']}").status_code in (200, 204)
    assert shown(api) == []
    again = book(api, "The Cart Book", "Ben Author", cart)
    api.get(f"/api/books/{again}/read-frontier")
    assert api.put(f"/api/books/{again}/read-chapters", json={"atoms": [1], "read": True}).status_code == 200
    assert shown(api) == []
    seen = models(app, monkeypatch, reply="Corvus moved lamp 22 in part 2.")
    ask(api, "And then?")
    assert OLD[0] not in seen["reply"][-1]


def test_a_reply_being_written_while_its_book_is_read_fresh_is_stored_but_not_shown_again(shelf, monkeypatch):
    """Catches: recording the books when the turn is stored rather than when its prompt was built.
    The reply below was written from the lamp book's text; by the time it is stored that text is
    forgotten, and a record taken then would say the turn drew on nothing of it."""
    api, app, _ = shelf
    models(app, monkeypatch, reply="Corvus moved lamp 21 in part 2.")
    write = app.state.client.complete

    def forget_then_write(system, user, tier="cheap", schema=None):
        if schema is None:
            app.state.catalog.clear_read_map(book_ids(app)["The Lamp Book"])
        return write(system, user, tier=tier, schema=schema)
    monkeypatch.setattr(app.state.client, "complete", forget_then_write)
    ask(api, "What did Corvus do with lamp 21?")
    assert len(app.state.catalog.get_turns(None, incarnation="home")) == 2
    assert shown(api) == []


# --- chapters gone through quickly, on the home screen --------------------------------------------
def test_the_home_chat_is_told_which_chapters_were_gone_through_quickly_and_nothing_that_is_on_them(
        shelf, monkeypatch):
    """Catches: the home chat still saying "not read with you" of a chapter skimmed here; anything
    of the skimmed pages but the chapter's number reaching a model; no button under the new wording."""
    from test_skimmed_pages import everything_sent
    api, app, _ = shelf
    lamps = book_ids(app)["The Lamp Book"]
    mark = {"cfi": "epubcfi(/6/2)", "position_epoch": 0, "atom": 1}
    assert api.put(f"/api/books/{lamps}/read-page", json={
        "start": {**mark, "quote": lamp(1, 3), "prefix": ""},
        "end": {**mark, "quote": lamp(1, 9), "prefix": lamp(1, 9)}, "skimmed": True}).status_code == 200
    reply = "We flipped through chapter 1 of The Lamp Book, so I didn’t take it in. Did you read it?"
    seen = models(app, monkeypatch, reply=reply)
    sent = everything_sent(app, monkeypatch)
    events = ask(api, "Where am I in The Lamp Book, and what did Jonas do with cart 7?")
    prompt = seen["reply"][-1]
    told = ("“The Lamp Book” by Ada Writer: the reader is in chapter 2 of 3 now. Read with you: part of "
            "chapter 2. The two of you went through chapter 1 quickly here, before where the reader is:")
    assert told in prompt and "Not read with you" not in prompt and "already brought this up" not in prompt
    assert all(told in " ".join(state["passages"]) for state in seen["checked"]) and seen["checked"]
    everything = "\n".join(sent)
    assert lamp(2, 21) in everything and cart(1, 7) in everything       # the models were reached
    assert "Mara" not in everything and "in part 1" not in everything
    assert [(o["title"], o["atoms"], o["chapters"]) for o in events[-1]["answer"]["offers"]] == [
        ("The Lamp Book", [1], "chapter 1")]
    # Asked once in this conversation: the next prompt says so.
    ask(api, "And in The Cart Book?")
    assert "You have already brought this up with the reader in this conversation" in seen["reply"][-1]
