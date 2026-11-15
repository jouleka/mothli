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
    assert "“Middlemarch” by George Eliot: on the shelf, not started" in prompt
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
    assert "“The Cart Book” by Ben Author: reading, 1 of 3 chapters finished" in prompt
    assert "“The Lamp Book” by Ada Writer: reading, 0 of 3 chapters finished" in prompt
    assert "“Deep Work” by Cal Newport: read elsewhere, finished 2022-02-01, rated 4" in prompt
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


def test_home_chat_needs_the_read_map_and_an_empty_shelf_calls_no_model(shelf, monkeypatch, tmp_path):
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
        assert [e["type"] for e in events] == ["done"] and events[0]["answer"]["insufficient_evidence"]
        assert seen["reply"] == []
