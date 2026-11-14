"""Reading history imported from other reading apps: merged idempotently, matched to the free
library, and never a way to send this server anything but titles, dates and the reader's own notes."""
import pytest
from fastapi.testclient import TestClient

from _epub import epub_ncx, three_chapter_book
from app import classics
from app.api import classics as classics_api
from app.config import Settings
from app.history import match_key
from app.main import create_app


@pytest.fixture
def c(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, allow_stub=True, data_dir=str(tmp_path / "data"))
    monkeypatch.setattr(classics_api, "fetch_classic", lambda entry, *, max_bytes, client=None: three_chapter_book())
    with TestClient(create_app(settings)) as client:
        yield client


GOODREADS = {"source": "goodreads", "books": [
    {"title": "Pride and Prejudice", "author": "Jane Austen", "isbn": "9780141439518", "status": "read",
     "rating": 5, "finished_on": "2021-05-03", "added_on": "2021-04-01", "review": "Still the best."},
    {"title": "Crime and Punishment", "author": "Fyodor Dostoevsky", "status": "want", "added_on": "2022-01-09"},
    {"title": "The Seven Husbands of Evelyn Hugo", "author": "Taylor Jenkins Reid", "status": "read", "rating": 4,
     "finished_on": "2023-08-20"},
]}
KINDLE = {"source": "kindle", "highlights": [
    {"title": "Pride and Prejudice (Penguin Classics)", "author": "Austen, Jane",
     "text": "It is a truth universally acknowledged", "location": "Location 12-13", "position": 12,
     "made_at": "2021-04-20T21:15:00"},
    {"title": "Pride and Prejudice (Penguin Classics)", "author": "Austen, Jane", "text": "", "note": "Lizzy at her best",
     "location": "Location 890", "position": 890},
    {"title": "Deep Work", "author": "Cal Newport", "text": "Clarity about what matters", "position": 40},
]}


def test_titles_from_different_apps_for_one_book_share_a_key():
    assert match_key("Pride and Prejudice (Penguin Classics)", "Austen, Jane") == match_key("Pride and Prejudice", "Jane Austen")
    assert match_key("The Hobbit: or, There and Back Again", "J.R.R. Tolkien") == match_key("Hobbit", "Tolkien, J. R. R.")
    assert match_key("Emma", "Jane Austen") != match_key("Emma", "Alexis Hall")


def test_library_matching_needs_title_and_author_and_forgives_a_spelling():
    assert classics.match_title("Pride and Prejudice", "Jane Austen") == "1342"
    assert classics.match_title("Crime and Punishment", "Fyodor Dostoevsky") == "2554"
    assert classics.match_title("The Great Gatsby", "Fitzgerald, F. Scott") == "64317"
    assert classics.match_title("Emma", "Alexis Hall") is None
    assert classics.match_title("Emma", "") is None
    assert classics.match_title("The Seven Husbands of Evelyn Hugo", "Taylor Jenkins Reid") is None


def test_an_import_lists_the_books_and_says_which_are_free(c):
    counts = c.post("/api/history/import", json=GOODREADS).json()
    assert counts == {"books_added": 3, "books_updated": 0, "highlights_added": 0, "skipped": 0}
    body = c.get("/api/history").json()
    assert body["books"] == 3 and body["free"] == 2 and body["by_status"]["read"] == 2 and body["by_status"]["want"] == 1
    newest = body["entries"][0]
    assert newest["title"] == "The Seven Husbands of Evelyn Hugo" and newest["classic_id"] is None
    austen = next(entry for entry in body["entries"] if entry["title"] == "Pride and Prejudice")
    assert austen["classic_id"] == "1342" and austen["book_id"] is None and austen["rating"] == 5
    assert austen["sources"] == ["goodreads"] and austen["has_review"] is True
    # once the reader adds it from the library, the history says it is on the shelf
    shelved = c.post("/api/classics/1342").json()["book_id"]
    assert c.get(f"/api/history/{austen['id']}").json()["book_id"] == shelved


def test_importing_twice_adds_nothing_and_a_second_source_fills_gaps(c):
    c.post("/api/history/import", json=GOODREADS)
    assert c.post("/api/history/import", json=GOODREADS).json() == {
        "books_added": 0, "books_updated": 0, "highlights_added": 0, "skipped": 0}
    counts = c.post("/api/history/import", json=KINDLE).json()
    assert counts["books_added"] == 1 and counts["highlights_added"] == 3
    assert c.post("/api/history/import", json=KINDLE).json()["highlights_added"] == 0
    body = c.get("/api/history", params={"only": "highlights"}).json()
    assert body["books"] == 4 and body["highlights"] == 3 and body["with_highlights"] == 2 and body["matches"] == 2
    austen = next(entry for entry in body["entries"] if entry["title"] == "Pride and Prejudice")
    assert austen["highlights"] == 2 and austen["sources"] == ["goodreads", "kindle"]
    detail = c.get(f"/api/history/{austen['id']}").json()
    assert detail["review"] == "Still the best."
    assert [(mark["text"], mark["note"]) for mark in detail["marks"]] == [
        ("It is a truth universally acknowledged", None), ("", "Lizzy at her best")]
    # a wish-list entry becomes "read" when another app says so, but a rating already here is kept
    again = {"source": "storygraph", "books": [
        {"title": "Crime and Punishment", "author": "Fyodor Dostoyevsky", "status": "read", "rating": 4.5},
        {"title": "Pride and Prejudice", "author": "Jane Austen", "status": "want", "rating": 2}]}
    assert c.post("/api/history/import", json=again).json()["books_updated"] == 2
    entries = {entry["title"]: entry for entry in c.get("/api/history").json()["entries"]}
    assert entries["Crime and Punishment"]["status"] == "read" and entries["Crime and Punishment"]["rating"] == 4.5
    assert entries["Pride and Prejudice"]["status"] == "read" and entries["Pride and Prejudice"]["rating"] == 5


def test_filtering_searching_and_forgetting(c):
    c.post("/api/history/import", json=GOODREADS)
    c.post("/api/history/import", json=KINDLE)
    assert c.get("/api/history", params={"status": "want"}).json()["matches"] == 1
    assert c.get("/api/history", params={"only": "free"}).json()["matches"] == 2
    found = c.get("/api/history", params={"q": "AUSTEN pride"}).json()
    assert found["matches"] == 1
    entry_id = found["entries"][0]["id"]
    assert c.delete(f"/api/history/{entry_id}").status_code == 204
    assert c.get(f"/api/history/{entry_id}").status_code == 404
    assert c.delete(f"/api/history/{entry_id}").status_code == 404
    assert c.get("/api/history").json()["highlights"] == 1
    assert c.delete("/api/history").status_code == 204
    assert c.get("/api/history").json()["books"] == 0


@pytest.mark.parametrize("bad", [
    {"source": "kindle"},
    {"source": "pirate-bay", "books": [{"title": "X", "author": "Y"}]},
    {"source": "goodreads", "books": [{"title": "", "author": "Y"}]},
    {"source": "goodreads", "books": [{"title": "X", "rating": 11}]},
    {"source": "goodreads", "books": [{"title": "X", "finished_on": "yesterday"}]},
    {"source": "goodreads", "books": [{"title": "X", "isbn": "'; DROP TABLE entries;--"}]},
    {"source": "goodreads", "books": [{"title": "X"}] * 501},
    {"source": "kindle", "highlights": [{"title": "X", "text": "y" * 9000}]},
])
def test_anything_but_plain_reading_records_is_refused(c, bad):
    assert c.post("/api/history/import", json=bad).status_code == 422
    assert c.get("/api/history").json()["books"] == 0


def test_a_highlight_with_no_words_and_a_book_with_no_real_title_are_left_out(c):
    counts = c.post("/api/history/import", json={"source": "kindle", "highlights": [
        {"title": "Deep Work", "author": "Cal Newport", "text": "   "},
        {"title": "(Unknown)", "author": "", "text": "orphan"}]}).json()
    assert counts["books_added"] == 0 and counts["highlights_added"] == 0


def test_a_book_the_reader_added_themselves_is_recognised_in_their_history(c):
    # not in the free library: the reader brings their own copy, and the history then opens it
    c.post("/api/history/import", json={"source": "goodreads", "books": [
        {"title": "The Seven Husbands of Evelyn Hugo: A Novel", "author": "Taylor Jenkins Reid", "status": "read"},
        {"title": "Synthetic", "author": "Somebody Else", "status": "want"}]})
    before = {entry["title"]: entry for entry in c.get("/api/history").json()["entries"]}
    assert all(entry["book_id"] is None and entry["classic_id"] is None for entry in before.values())
    own = epub_ncx([("c1.xhtml", "One", "One", "Evelyn tells her story. " * 30)],
                   title="The Seven Husbands of Evelyn Hugo", author="Taylor Jenkins Reid")
    book_id = c.post("/api/books", files={"file": ("evelyn.epub", own, "application/epub+zip")}).json()["book_id"]
    after = {entry["title"]: entry for entry in c.get("/api/history").json()["entries"]}
    assert after["The Seven Husbands of Evelyn Hugo: A Novel"]["book_id"] == book_id
    # a different author's book of the same name is not mistaken for it
    assert after["Synthetic"]["book_id"] is None
    other = epub_ncx([("c1.xhtml", "One", "One", "A different book entirely. " * 30)], title="Synthetic", author="Jane Doe")
    c.post("/api/books", files={"file": ("other.epub", other, "application/epub+zip")})
    assert c.get(f"/api/history/{after['Synthetic']['id']}").json()["book_id"] is None


def test_the_reader_can_say_which_shelf_book_is_their_copy(c):
    c.post("/api/history/import", json={"source": "goodreads", "books": [
        {"title": "Tomorrow, and Tomorrow, and Tomorrow", "author": "Gabrielle Zevin", "status": "reading"}]})
    entry = c.get("/api/history").json()["entries"][0]
    # the file calls itself something else entirely, so only the reader's say-so links it
    own = epub_ncx([("c1.xhtml", "One", "One", "Sam and Sadie make a game. " * 30)], title="TTT final", author="unknown")
    book_id = c.post("/api/books", files={"file": ("ttt.epub", own, "application/epub+zip")}).json()["book_id"]
    assert c.get(f"/api/history/{entry['id']}").json()["book_id"] is None
    linked = c.put(f"/api/history/{entry['id']}/book", json={"book_id": book_id})
    assert linked.status_code == 200 and linked.json()["book_id"] == book_id
    assert c.get("/api/history").json()["entries"][0]["book_id"] == book_id
    assert "own_book_id" not in c.get("/api/history").json()["entries"][0]
    assert c.put(f"/api/history/{entry['id']}/book", json={"book_id": "nope"}).status_code == 404
    assert c.put("/api/history/9999/book", json={"book_id": book_id}).status_code == 404
    # once the book is taken off the shelf, the history no longer offers to open it
    assert c.delete(f"/api/books/{book_id}").status_code == 204
    assert c.get(f"/api/history/{entry['id']}").json()["book_id"] is None


def test_a_glimpse_is_one_of_the_readers_own_marked_passages(c):
    assert c.get("/api/history/glimpse").json() == {"mark": None}
    c.post("/api/history/import", json={"source": "kindle", "highlights": [
        {"title": "Deep Work", "author": "Cal Newport", "text": "too short"},
        {"title": "Deep Work", "author": "Cal Newport", "text": "", "note": "a note alone is not a passage to quote back"},
        {"title": "Deep Work", "author": "Cal Newport",
         "text": "Clarity about what matters provides clarity about what does not."}]})
    mark = c.get("/api/history/glimpse").json()["mark"]
    assert mark["text"].startswith("Clarity about what matters") and mark["title"] == "Deep Work"
    assert mark["author"] == "Cal Newport" and isinstance(mark["entry_id"], int)
