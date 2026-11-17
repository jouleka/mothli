"""Removing a book from the shelf in hosted mode, against real PostgreSQL, through the hosted HTTP
app on the RLS-enforced tenant role: what goes, what is kept out of sight, and that nothing of a
removed book (its chapter notes, what Mothli remembers from it, its conversation) ever comes back in
a reply, in the book chat of the same book added again or in the home chat. The app's confirmation
(frontend/src/shelf/RemoveBook.tsx) says exactly this. Stand-in writer and Jev only.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.chapter_notes import VERSION, text_hash
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.models import OwnerId
from app.read_frontier import source_state
from app.recollections import STORE_CAP
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_companion_notes_postgres import rows
from tests.hosted.test_companion_recollections_postgres import (
    HOME_CHAT,
    HomeModels,
    cart_book,
    kept,
    reader,
    said,
)
from tests.hosted.test_companion_stream_postgres import adapters, restore_caps  # noqa: F401

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
lamp_book = read_map_gate.lamp_book
line = read_map_gate.line
upload = read_map_gate.upload

NOTE = "Mara kept the cobalt lamp in a red tin."          # Mothli's note on chapter 1
TIED = "The reader finds Mara unbearable."                 # what Mothli remembers from the book
UNTIED = "The reader reads at night, on the train home."  # what Mothli remembers about the reader
TALK = "Honestly, Mara is the worst person in this book."  # the reader, in the book's chat
QUESTION = "What did Mara do with the lamps?"


@pytest.fixture()
def notes_app(tenant_dsn: str, readers: dict, tmp_path):
    """The home chat's app with chapter notes on, so a reply is given them."""
    from datetime import UTC, datetime

    from app.main import create_app
    from tests.hosted.test_tenant_postgres import UnusedOIDCClient

    tokens, sessions = read_map_gate.signed_in(readers)
    objects = tmp_path / "objects"
    app = create_app(
        read_map_gate.hosted_settings(tenant_dsn, objects, **{**HOME_CHAT, "reader_story_notes": True}),
        auth_repository=sessions, oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app, objects


def book_prompt(models: HomeModels) -> str:
    return [prompt for prompt in models.prompts if QUESTION in prompt][-1]


def test_a_removed_book_goes_or_is_kept_out_of_sight_and_never_comes_back_in_a_reply(
        notes_app, readers, database, monkeypatch):
    """The break it catches: a removed book's chapter notes, what Mothli remembers from it, or its
    conversation given to a later reply (the same book added again, or the home chat); its file
    left in storage; its highlights still listed; or a removal that erases what the confirmation
    says is kept out of sight, or the notes about the reader that are not about the book."""
    client, tokens, app, objects = notes_app
    a = reader((client, tokens, app), readers)
    lamps = upload(client, tokens, "a", lamp_book())
    upload(client, tokens, "a", cart_book())          # the shelf is not empty afterwards
    a.mark(lamps, [1])
    catalog, source = adapters(app, a.owner)
    _state, _book, _manifest, chapters = source_state(lamps, catalog, app.state.settings, source)
    catalog.set_chapter_notes(lamps, 1, source_hash=text_hash(chapters[0]["text"]), version=VERSION,
                              sentences=[NOTE], dropped=0)
    remembered = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    tied = kept([("taste", TIED)])
    tied[0].update(tied=True, book_id=lamps)
    remembered.keep_recollections(tied + kept([("about", UNTIED)]), cap=STORE_CAP)
    said(app, a.owner, lamps, TALK)
    highlight = a.call("POST", f"/api/books/{lamps}/highlights", {
        "anchor": {"cfi": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "atom": 1}, "color": "yellow",
        "selected_text": "Mara"})
    assert highlight.status_code == 201, highlight.text

    # Before: each of them reaches a reply (so their absence later means something).
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a.ask(lamps, QUESTION)
    before = book_prompt(models)
    assert NOTE in before and TIED in before and TALK in before and UNTIED in before
    a.ask_home(QUESTION)
    assert TIED in book_prompt(models)
    stored = rows(database, "SELECT storage_key FROM source_objects WHERE owner_id=%s AND book_id=%s",
                  a.owner, uuid.UUID(lamps))[0]["storage_key"]
    stored_file = objects / a.owner.hex / f"{stored}.epub.enc"
    assert stored_file.exists()

    assert a.call("DELETE", f"/api/books/{lamps}").status_code == 204

    # What goes: the book's file from storage; the book, its highlights and its pages for the reader.
    assert not stored_file.exists()
    assert lamps not in [book["book_id"] for book in a.call("GET", "/api/books").json()]
    for path in ("", "/epub", "/manifest", "/position", "/marks", "/read-map", "/conversation"):
        assert a.call("GET", f"/api/books/{lamps}{path}").status_code == 404, path
    # What is kept out of sight, as the confirmation says: still stored, never shown.
    book_id = uuid.UUID(lamps)
    for table in ("reading_state", "conversation_turns", "chapter_notes"):
        assert rows(database, f"SELECT 1 FROM {table} WHERE owner_id=%s AND book_id=%s",
                    a.owner, book_id), table
    assert rows(database, "SELECT text FROM recollections WHERE owner_id=%s AND book_id=%s",
                a.owner, book_id) == [{"text": TIED}]
    assert rows(database, "SELECT deleted_at IS NOT NULL AS gone FROM highlights "
                          "WHERE owner_id=%s AND book_id=%s", a.owner, book_id) == [{"gone": True}]
    # What stays: the note about the reader, and nothing tied to the book, in the list.
    assert [note["text"] for note in a.notes_listed()["recollections"]] == [UNTIED]

    # The home chat: nothing of the removed book.
    a.ask_home(QUESTION)
    home = book_prompt(models)
    for gone in (NOTE, TIED, TALK):
        assert gone not in home, gone

    # The same file added again is a new book, started from the beginning, with none of it.
    again = upload(client, tokens, "a", lamp_book())
    assert again != lamps
    assert a.call("GET", f"/api/books/{again}/position").json()["bookmark"] == 0
    assert a.shown(again) == []
    assert a.call("GET", f"/api/books/{again}/marks").json()["marks"] == []
    a.mark(again, [1])
    a.ask(again, QUESTION)
    after = book_prompt(models)
    for gone in (NOTE, TIED, TALK):
        assert gone not in after, gone
    assert UNTIED in after and line(1, 2) in after
