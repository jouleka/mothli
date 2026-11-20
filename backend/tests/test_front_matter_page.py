"""Project Gutenberg's own page is not a chapter (audit 6am, 17 November).

A first-run reader of "Alice's Adventures in Wonderland" (Gutenberg book 11) met a book split
into the cover, a piece titled "The Project Gutenberg eBook of Alice's Adventures in Wonderland"
(Gutenberg's header, the title page and the contents) and the twelve chapters. That piece was
counted as chapter 1: Mothli called Chapter I "chapter 2", the app congratulated the reader on
finishing it, and the pages of its header were refused when reported.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "api"))
sys.path.insert(0, str(Path(__file__).parent / "ingest" / "extraction"))
from _alice_shaped import CONTENTS, HEADER, alice_shaped_epub, line  # noqa: E402
from _epub import epub_ncx  # noqa: E402
from test_chapter_notes import models  # noqa: E402
from test_read_map import _opened, ask  # noqa: E402

from app.chapter_notes import build_missing  # noqa: E402
from app.content_scope import is_content  # noqa: E402
from app.ingest.extraction.chapter_text import segment_for_ingest  # noqa: E402
from app.reader_place import content_numbers, place_line  # noqa: E402

ALICE_FILE = Path(__file__).resolve().parents[2] / "books" / "pg11.epub"
# The manifest the app made of the real book (titles and lengths as stored on the reader's shelf).
ALICE_ATOMS = [{"ordinal": 1, "title": "(untitled)", "char_len": 1},
               {"ordinal": 2, "title": "The Project Gutenberg eBook of Alice's Adventures in Wonderland",
                "char_len": 554}] + [
    {"ordinal": n + 2, "title": f"CHAPTER {roman}. {name}", "char_len": 11000}
    for n, (roman, name) in enumerate([
        ("I", "Down the Rabbit-Hole"), ("II", "The Pool of Tears"), ("III", "A Caucus-Race and a Long Tale"),
        ("IV", "The Rabbit Sends in a Little Bill"), ("V", "Advice from a Caterpillar"),
        ("VI", "Pig and Pepper"), ("VII", "A Mad Tea-Party"), ("VIII", "The Queen’s Croquet-Ground"),
        ("IX", "The Mock Turtle’s Story"), ("X", "The Lobster Quadrille"), ("XI", "Who Stole the Tarts?"),
        ("XII", "Alice’s Evidence")], 1)]


def test_gutenbergs_own_pages_are_not_chapters_and_a_books_own_chapters_stay():
    """The break it catches: "The Project Gutenberg eBook of ..." counted as a chapter of the book.

    Recognised by Gutenberg's own wording at the start of the title, never by a word alone: a
    chapter that merely mentions a project, or Gutenberg, stays a chapter.
    """
    for title in ["The Project Gutenberg eBook of Alice's Adventures in Wonderland",
                  "The Project Gutenberg EBook of Pride and Prejudice, by Jane Austen",
                  "Project Gutenberg's Alice's Adventures in Wonderland, by Lewis Carroll",
                  "Project Gutenberg’s The Brothers Karamazov", "The Project Gutenberg Etext of Hamlet",
                  "THE FULL PROJECT GUTENBERG LICENSE", "The Full Project Gutenberg™ License",
                  "End of the Project Gutenberg EBook of Alice's Adventures in Wonderland",
                  "End of Project Gutenberg's Alice's Adventures in Wonderland"]:
        assert not is_content({"title": title, "char_len": 500}), title
    for title in ["The Project", "Project Mercury", "Gutenberg", "The Gutenberg Press",
                  "Chapter 3. The Project Gutenberg Never Finished", "CHAPTER I. Down the Rabbit-Hole",
                  "Projects", "The End of the Project"]:
        assert is_content({"title": title, "char_len": 500}), title


def test_mothli_calls_chapter_one_of_alice_chapter_1_of_12():
    """The break it catches: Mothli saying "chapter 2" (of 13) on the first page of Chapter I.

    Stored manifests are judged by their titles, so this holds for a copy already on a shelf.
    """
    numbers = content_numbers({"atoms": ALICE_ATOMS})
    assert numbers == {ordinal: ordinal - 2 for ordinal in range(3, 15)}
    item = {"book": {"title": "Alice's Adventures in Wonderland", "author": "Lewis Carroll"},
            "chapters": [{"ordinal": 3, "whole": False, "text": "Alice was beginning to get very tired."
                          " of sitting by her sister on the bank."}],
            "numbers": numbers, "point": {"atom": 3}}
    assert "the reader is in chapter 1 of 12 now" in place_line(item)


@pytest.mark.skipif(not ALICE_FILE.exists(), reason="books/pg11.epub (Alice) not present")
def test_the_real_alice_file_has_twelve_chapters_and_its_gutenberg_page_is_none_of_them():
    _result, chapters = segment_for_ingest(str(ALICE_FILE), "alice")
    numbers = content_numbers({"atoms": [{**c, "char_len": len(c["text"]) or 1} for c in chapters]})
    assert len(chapters) == 14 and len(numbers) == 12
    assert chapters[1]["title"].startswith("The Project Gutenberg eBook of") and 2 not in numbers
    assert numbers[3] == 1 and chapters[2]["title"].startswith("CHAPTER I.")


@pytest.fixture
def alice_env(tmp_path):
    yield from _opened(tmp_path, alice_shaped_epub())


def mark(atom, quote, prefix=""):
    return {"atom": atom, "quote": quote, "prefix": prefix, "cfi": "epubcfi(/6/4)", "position_epoch": 0}


def page(api, bid, start, end):
    return api.put(f"/api/books/{bid}/read-page", json={"start": start, "end": end})


def state(api, bid):
    return (api.get(f"/api/books/{bid}/read-map").json(), api.get(f"/api/books/{bid}/position").json(),
            api.get(f"/api/books/{bid}/read-frontier").json())


def test_the_manifest_numbers_the_books_own_chapters_and_leaves_the_rest_unnumbered(alice_env):
    """The break it catches: the reader app counting every piece ("Chapter 3 of 14" on Chapter I).

    The app is given the same numbering as Mothli: none for the cover and the Gutenberg page.
    """
    api, bid, _, _ = alice_env
    manifest = api.get(f"/api/books/{bid}/manifest").json()
    assert manifest["atoms"][1]["title"].startswith("The Project Gutenberg eBook of")
    assert [atom["content_number"] for atom in manifest["atoms"]] == [None, None, 1, 2, 3]
    assert manifest["content_count"] == 3
    assert manifest["atoms"][1]["content_role"] == "supplementary"


def test_a_book_whose_every_piece_is_a_chapter_is_numbered_as_before(tmp_path):
    """A guard (Pride and Prejudice's shape: every piece a chapter): its chapter numbers are its
    pieces' numbers, so the app says what it said before ("Chapter 3 of 61" stays)."""
    epub = epub_ncx([(f"c{n}.xhtml", f"Chapter {n}", f"Chapter {n}", f"Elizabeth walked to town {n}. " * 30)
                     for n in range(1, 7)], title="Pride and Prejudice", author="Jane Austen")
    for api, bid, _, _ in _opened(tmp_path, epub):
        manifest = api.get(f"/api/books/{bid}/manifest").json()
        assert [atom["content_number"] for atom in manifest["atoms"]] == [1, 2, 3, 4, 5, 6]
        assert manifest["content_count"] == 6 == manifest["content_chapters"]


def test_pages_of_the_gutenberg_page_are_never_refused_and_never_read_text(alice_env, monkeypatch):
    """The break it catches: the two refused page reports of the first run (422), and a page of
    the Gutenberg page counted as read text of a chapter.

    A page of Gutenberg's header holds text the server keeps out of every chapter, so it could
    not be placed and was refused; a page of its contents was taken as chapter 1 read. Now both
    are accepted and add nothing: no chapter read, the reader's place unmoved, nothing offered.
    """
    api, bid, app, _ = alice_env
    seen = models(app, monkeypatch, reply="Hello.")
    fresh = state(api, bid)
    header = page(api, bid, mark(2, HEADER), mark(2, HEADER, HEADER))
    assert header.status_code == 200, header.text
    contents = page(api, bid, mark(2, CONTENTS), mark(2, CONTENTS, CONTENTS))
    assert contents.status_code == 200, contents.text
    assert contents.json().get("skipped") is None
    assert state(api, bid) == fresh and fresh[1]["place_chapter"] == 0
    assert [chapter["atom"] for chapter in fresh[0]["chapters"]] == [3, 4, 5]

    # The first page of Chapter I, as the reader app reports it (the heading is text too).
    opening = page(api, bid, mark(3, "CHAPTER I. Down the Lamp-Hole " + line(1, 1)), mark(3, line(1, 20), line(1, 20)))
    assert opening.status_code == 200, opening.text
    assert opening.json()["skipped"] is None                 # nothing before Chapter I to ask about
    read_map, position, _ = state(api, bid)
    assert position["place_chapter"] == 3 and read_map["chapters"][0]["read"] > 0
    ask(api, bid)
    assert "the reader is in chapter 1 of 3 now" in seen["reply"][-1]
    assert not any("Project Gutenberg" in prompt or "LANTERN EDITION" in prompt for prompt in seen["reply"])


def test_the_gutenberg_page_is_never_a_finished_chapter_and_chapter_one_is(alice_env, monkeypatch):
    """The break it catches: "That's The Project Gutenberg eBook of ... done!" and notes for it.

    The app offers the "chapter finished" note for the piece at the chapter counter when it moves
    and that piece is a chapter (content_number). Passing over the cover and the Gutenberg page
    moves the counter to the Gutenberg page, which is not one; reading Chapter I moves it to
    Chapter I, which is. No unread chapter is ever counted.
    """
    api, bid, app, settings = alice_env
    settings.reader_story_notes = True
    models(app, monkeypatch, notes="Mara moved lamps in part 1.", reply="Hello.")
    numbered = [atom["content_number"] for atom in api.get(f"/api/books/{bid}/manifest").json()["atoms"]]
    bookmark = api.get(f"/api/books/{bid}/position").json()["bookmark"]
    assert bookmark < 3 and numbered[bookmark - 1] is None
    page(api, bid, mark(3, "CHAPTER I. Down the Lamp-Hole " + line(1, 1)), mark(3, line(1, 60), line(1, 60)))
    bookmark = api.get(f"/api/books/{bid}/position").json()["bookmark"]
    assert bookmark == 3 and numbered[bookmark - 1] == 1
    state_ = app.state
    build_missing(bid, state_.catalog, state_.store, settings, state_.client, state_.worker)
    assert [note["atom"] for note in state_.catalog.get_chapter_notes(bid)] == [3]


def test_finishing_a_chapter_by_its_pages_starts_its_extraction_at_once(alice_env, monkeypatch):
    """The break it catches: "Tell me" after a finished chapter answering "I'm still taking notes"
    until some later page turn happened to start the chapter's extraction."""
    api, bid, app, _ = alice_env
    queued = []
    monkeypatch.setattr(app.state.worker, "enqueue", lambda book_id, through: queued.append((book_id, through)))
    page(api, bid, mark(3, "CHAPTER I. Down the Lamp-Hole " + line(1, 1)), mark(3, line(1, 30), line(1, 30)))
    assert queued == []                                       # nothing finished yet
    page(api, bid, mark(3, line(1, 31)), mark(3, line(1, 60), line(1, 60)))
    assert queued == [(bid, 3)]
