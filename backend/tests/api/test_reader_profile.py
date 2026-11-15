"""The reader profile: where the reader is in each book, what they have said they felt, and what
they have read elsewhere. Computed from stored state; no model writes any of it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from test_page_read_frontier import page_env as page_env, confirm  # noqa: E402,F401
from test_chapter_notes import models  # noqa: E402


def profile(api):
    response = api.get("/api/reader/profile")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_books_status_follows_the_reading_position(page_env):
    api, bid, app, _ = page_env
    assert [(b["book_id"], b["status"], b["chapters_read"], b["chapters"]) for b in profile(api)["books"]] == [
        (bid, "unread", 0, 2)]
    confirm(api, bid)                                   # inside chapter one
    assert [(b["status"], b["chapters_read"]) for b in profile(api)["books"]] == [("reading", 0)]
    app.state.catalog.set_position(bid, "epubcfi(/6/2)", 1)
    assert [(b["status"], b["chapters_read"]) for b in profile(api)["books"]] == [("reading", 1)]
    app.state.catalog.set_position(bid, "epubcfi(/6/4)", 2)
    assert [(b["status"], b["chapters_read"]) for b in profile(api)["books"]] == [("finished", 2)]


def test_reactions_are_the_readers_own_words_and_only_up_to_the_current_position(page_env, monkeypatch):
    api, bid, app, settings = page_env
    settings.reader_conversation_jev = settings.reader_conversation_stream = True
    point = confirm(api, bid)
    models(app, monkeypatch, reply="It's the red tin that gets me.", reaction=True)
    api.post(f"/api/books/{bid}/ask/stream", json={"question": "Poor Mara!", "read_frontier": point})
    assert profile(api)["books"][0]["reactions"] == ["Poor Mara!"]
    app.state.catalog.reset_position(bid, expected_epoch=0)          # starting over
    book = profile(api)["books"][0]
    assert book["reactions"] == [] and book["status"] == "unread"


def test_books_read_elsewhere_are_listed_with_what_the_reader_recorded(page_env):
    api, bid, _, _ = page_env
    api.post("/api/history/import", json={"source": "goodreads", "books": [
        {"title": "Middlemarch", "author": "George Eliot", "status": "read", "rating": 5, "finished_on": "2021-05-03"},
        {"title": "Deep Work", "author": "Cal Newport", "status": "want"}]})
    elsewhere = profile(api)["elsewhere"]
    assert [(e["title"], e["author"], e["status"], e["rating"], e["finished_on"]) for e in elsewhere] == [
        ("Middlemarch", "George Eliot", "read", 5.0, "2021-05-03"), ("Deep Work", "Cal Newport", "want", None, None)]
