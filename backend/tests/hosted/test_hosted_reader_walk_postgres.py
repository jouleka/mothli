"""What the first end-to-end walk of hosted mode found, against real PostgreSQL: the hosted app as
the frontend calls it, on the RLS-enforced tenant role, with reader keys off (the shipped default)
and no story memory (no extraction runs for hosted books yet, ADR 0041 part 10).
"""

from __future__ import annotations

import uuid

import pytest

from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_free_library_postgres as library_gate

pytestmark = pytest.mark.postgres

# The free library's app: hosted settings as shipped, reader keys off, the network refused.
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
no_network = library_gate.no_network
library_app = library_gate.library_app
as_reader = library_gate.as_reader
made_up_book = library_gate.made_up_book
upload = library_gate.upload


def put_position(app, label: str, book: str, completed: int, sequence: int = 1, version: int = 0):
    return app[0].put(f"/api/books/{book}/position", headers=as_reader(app, label), json={
        "cfi": "epubcfi(/6/4!/4/2/1:0)", "offset": 100 * completed, "completed_chapter": completed,
        "position_epoch": 0, "base_version": version, "client_id": str(uuid.uuid4()),
        "client_sequence": sequence})


def test_a_place_past_the_first_chapter_is_kept_for_a_book_with_no_story_memory(library_app):
    """The break it catches: the place checked against the chapters extraction records, which a
    hosted book does not have, so every page turned past chapter 1 is refused with 422
    "completed chapter is outside the current book" and the reader's place is never kept."""
    book = upload(library_app, "a", made_up_book()).json()["book_id"]
    saved = put_position(library_app, "a", book, 2)
    assert saved.status_code == 200, saved.text
    assert saved.json()["bookmark"] == 2
    again = library_app[0].get(f"/api/books/{book}/position", headers=as_reader(library_app, "a"))
    assert again.json()["bookmark"] == 2 and again.json()["cfi"] == "epubcfi(/6/4!/4/2/1:0)"
    # Past the book's last chapter (it has two) is still refused.
    beyond = put_position(library_app, "a", book, 3, sequence=2, version=saved.json()[
        "position_version"])
    assert beyond.status_code == 422


# The local manifest's shape (app/api/books.py atom_manifest), which the reader reads.
LOCAL_MANIFEST = {"book_id", "atom_set_version", "mode", "book_profile", "content_language",
                  "content_chapters", "content_count", "atoms"}
LOCAL_ATOM = {"ordinal", "href", "title", "part_label", "char_len", "chapter_number",
              "content_number", "content_role"}


def test_the_manifest_has_the_local_shape_and_shows_the_title_of_a_chapter_being_read(
        library_app):
    """The break it catches: the hosted manifest missing the chapter numbers the reader counts
    with, and hiding the title of a chapter the reader is reading but has not finished the one
    before (a chapter marked not read): local mode shows a title once any of it is read."""
    client = library_app[0]
    book = upload(library_app, "a", read_map_gate.lamp_book()).json()["book_id"]
    manifest = client.get(f"/api/books/{book}/manifest", headers=as_reader(library_app, "a")).json()
    assert set(manifest) == LOCAL_MANIFEST
    assert all(set(atom) == LOCAL_ATOM for atom in manifest["atoms"])
    assert manifest["content_count"] == 3
    assert [atom["content_number"] for atom in manifest["atoms"]] == [1, 2, 3]
    assert [atom["title"] for atom in manifest["atoms"]] == ["Part 1", "", ""]
    # Five lines of chapter 2 read, chapter 1 not: chapter 2's title shows, chapter 3's does not.
    read = client.put(f"/api/books/{book}/read-page", headers=as_reader(library_app, "a"),
                      json=read_map_gate.page(2, 1, 5))
    assert read.status_code == 200, read.text
    manifest = client.get(f"/api/books/{book}/manifest", headers=as_reader(library_app, "a")).json()
    assert [atom["title"] for atom in manifest["atoms"]] == ["Part 1", "Part 2", ""]
    assert [atom["content_role"] for atom in manifest["atoms"]] == ["content", "content", None]
