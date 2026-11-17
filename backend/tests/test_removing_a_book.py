"""Removing a book from the shelf in local mode: what goes, what stays on disk, and that nothing of
a removed book (its chapter notes, what Mothli remembers from it, its conversation) ever comes back
in a reply, in the book chat of the same file added again or in the home chat. The app's
confirmation (frontend/src/shelf/RemoveBook.tsx) says exactly this. Stand-in writer and Jev only;
the hosted twin is tests/hosted/test_removing_a_book_postgres.py.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "api"))
from _epub import epub_ncx  # noqa: E402
from test_home_recollections import remember  # noqa: E402
from test_read_map import line, map_env as map_env  # noqa: E402,F401
from test_recollections import models, said  # noqa: E402

from app.chapter_notes import VERSION, text_hash  # noqa: E402
from app.read_frontier import source_state  # noqa: E402

NOTE = "Mara kept the cobalt lamp in a red tin."          # Mothli's note on chapter 1
TIED = "The reader finds Mara unbearable."                 # what Mothli remembers from the book
UNTIED = "The reader reads at night, on the train home."  # what Mothli remembers about the reader
TALK = "Honestly, Mara is the worst person in this book."  # the reader, in the book's chat
QUESTION = "What did Mara do with the lamps?"


def other_file():
    return epub_ncx([(f"c{n}.xhtml", f"Leg {n}", f"Leg {n}",
                      " ".join(f"Jonas pushed cart {i} in leg {n}." for i in range(1, 41))) for n in (1, 2)],
                    title="The Cart Book", author="Ben Author")


def mark_read(api, bid):
    response = api.put(f"/api/books/{bid}/read-chapters", json={"atoms": [1], "read": True})
    assert response.status_code == 200, response.text


def ask(api, bid):
    point = api.get(f"/api/books/{bid}/read-frontier").json()
    response = api.post(f"/api/books/{bid}/ask/stream", json={"question": QUESTION, "read_frontier": point})
    assert response.status_code == 200, response.text


def ask_home(api):
    response = api.post("/api/reader/ask/stream", json={"question": QUESTION})
    assert response.status_code == 200, response.text
    assert [json.loads(row[6:]) for row in response.text.splitlines() if row.startswith("data: ")]


def prompt_of(seen):
    return [prompt for prompt in seen["reply"] if QUESTION in prompt][-1]


def test_a_removed_book_goes_and_never_comes_back_in_a_reply(map_env, monkeypatch):
    """The break it catches: a removed book's chapter notes, what Mothli remembers from it, or its
    conversation given to a later reply (the same file added again, which locally is the same
    book id, or the home chat); its place kept; or a removal that loses the notes about the reader
    that are not about the book, or the book's file and story memory the confirmation says stay."""
    api, bid, app, settings = map_env
    settings.reader_recollections = True
    catalog = app.state.catalog
    assert api.post("/api/books", files={"file": ("c.epub", other_file(), "application/epub+zip")}).status_code == 201
    mark_read(api, bid)                                 # chapter 1, read in full
    settings.reader_story_notes = True
    _state, _book, _manifest, chapters = source_state(bid, catalog, settings, app.state.worker)
    catalog.set_chapter_notes(bid, 1, source_hash=text_hash(chapters[0]["text"]), version=VERSION,
                              sentences=[NOTE], dropped=0)
    remember(app, TIED, kind="taste", book=bid)
    remember(app, UNTIED)
    said(app, bid, TALK)

    # Before: each of them reaches a reply (so their absence later means something).
    seen = models(app, monkeypatch, reply=line(1, 2))
    ask(api, bid)
    before = prompt_of(seen)
    assert NOTE in before and TIED in before and TALK in before and UNTIED in before
    book_dir = os.path.join(settings.data_dir, "books", bid)
    kept_files = {"source.epub", "atoms.json", "memory.db"}
    assert kept_files <= set(os.listdir(book_dir))

    assert api.delete(f"/api/books/{bid}").status_code == 204

    # What goes: the book from the shelf, its place, its conversation, its notes and what Mothli
    # remembers from it, from the catalogue itself.
    assert bid not in [book["book_id"] for book in api.get("/api/books").json()]
    for path in ("/epub", "/manifest", "/position", "/read-map", "/conversation"):
        assert api.get(f"/api/books/{bid}{path}").status_code == 404, path
    assert catalog.get_chapter_notes(bid) == [] and catalog.get_state(bid) is None
    assert catalog.get_book(bid) is None
    with catalog._lock:
        assert catalog._conn.execute("SELECT count(*) FROM conversation_turns WHERE book_id=?",
                                     (bid,)).fetchone()[0] == 0
    # What stays: the file and what Mothli learned from its pages, on disk; the note about the reader.
    assert kept_files <= set(os.listdir(book_dir))
    assert [note["text"] for note in catalog.get_recollections()] == [UNTIED]

    # The home chat: nothing of the removed book.
    ask_home(api)
    home = prompt_of(seen)
    for gone in (NOTE, TIED, TALK):
        assert gone not in home, gone

    # The same file added again: the same id here, started from the beginning, with none of it.
    with open(os.path.join(book_dir, "source.epub"), "rb") as kept_file:
        same = kept_file.read()
    again = api.post("/api/books", files={"file": ("b.epub", same, "application/epub+zip")})
    assert again.status_code == 201, again.text
    assert again.json()["book_id"] == bid
    assert api.get(f"/api/books/{bid}/position").json()["bookmark"] == 0
    assert api.get(f"/api/books/{bid}/conversation").json()["turns"] == []
    mark_read(api, bid)
    ask(api, bid)
    after = prompt_of(seen)
    for gone in (NOTE, TIED, TALK):
        assert gone not in after, gone
    assert UNTIED in after and line(1, 2) in after
