"""Removing a book from the shelf in hosted mode, against real PostgreSQL, through the hosted HTTP
app on the RLS-enforced tenant role: everything about that book's reading is erased in the same
transaction that takes it off the shelf, what must stay stays (what its replies cost, its cancelled
jobs, the audit trail, the book's row as a bare id they refer to), nothing of another reader moves,
a removal interrupted between storage and database can simply be asked again, and nothing of the
removed book (its chapter notes, what Mothli remembers from it, its conversation) ever comes back
in a reply, in the book chat of the same book added again or in the home chat. The app's
confirmation (frontend/src/shelf/RemoveBook.tsx) says exactly this. Stand-in writer and Jev only.

Which tables are erased is found from the database's own catalogue: every table with an owner and
a book is either erased by ``remove_book`` (``ERASED_WITH_THE_BOOK``, or through a cascade from one
of those), or kept here with its reason, or a story memory table, which holds no reader's rows
(ADR 0044). A table a later migration adds is one or the other, or
``test_every_table_about_a_book_is_erased_or_kept_for_a_reason`` fails.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from app.chapter_notes import VERSION, text_hash
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import ERASED_WITH_THE_BOOK
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
LEANT = "The reader keeps a notebook of lamps."            # about the reader, said in the book
TALK = "Honestly, Mara is the worst person in this book."  # the reader, in the book's chat
HOME_BEFORE = "Hello Mothli, nothing read yet."            # the home chat, before any reading
HOME_DURING = "Mara again, at home this time."             # the home chat, leaning on the book
QUESTION = "What did Mara do with the lamps?"

# Tables with an owner and a book that removing a book keeps, and why. Everything else with an
# owner and a book must hold no row of the removed book afterwards.
KEPT = {
    "cost_ledger": "what the book's replies cost: the owner's money record",
    "cost_reservations": "what the book's replies held against the caps: the owner's money record",
    "jobs": "its background work, cancelled; the money records refer to it; no words in it",
}
# Tables with an owner and a book that hold no reader's rows at all: a book's story memory is its
# edition's, rows of the notes account, never a reader's (ADR 0044), and the tenant role holds no
# privilege on them (step 7). Removing a book leaves the edition for the other readers of the file
# and clears the reader's link to it (the fingerprints on their book's row).
NOT_A_READERS = {
    table: "a book's story memory: the notes account's edition, never a reader's"
    for table in ("chapters", "ingested_chapters", "chapter_summaries", "entities", "aliases",
                  "entity_state", "edges", "events", "themes", "event_participants",
                  "entity_corrections", "chunks", "chunk_embeddings", "name_words")
}


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


def owner_book_tables(conn) -> list[str]:
    """Every table with an owner and a book, from the database's own catalogue."""
    return [row[0] for row in conn.execute(
        """SELECT c.table_name FROM information_schema.columns AS c
           JOIN information_schema.tables AS t USING (table_schema, table_name)
           WHERE c.table_schema='public' AND t.table_type='BASE TABLE'
             AND c.column_name='book_id'
             AND EXISTS (SELECT 1 FROM information_schema.columns AS o
                         WHERE o.table_schema='public' AND o.table_name=c.table_name
                           AND o.column_name='owner_id')
           ORDER BY 1""").fetchall()]


def cascades_from(conn, tables) -> set[str]:
    """The tables whose rows go when rows of ``tables`` are deleted (ON DELETE CASCADE, followed
    as far as it reaches), from the catalogue."""
    reached, frontier = set(), set(tables)
    while frontier:
        children = {row[0] for row in conn.execute(
            """SELECT child.relname FROM pg_constraint AS fk
               JOIN pg_class AS child ON child.oid=fk.conrelid
               JOIN pg_class AS parent ON parent.oid=fk.confrelid
               WHERE fk.contype='f' AND fk.confdeltype='c' AND parent.relname = ANY(%s)""",
            (sorted(frontier),)).fetchall()}
        frontier = children - reached - set(tables)
        reached |= frontier
    return reached


def about(conn, table: str, owner: uuid.UUID, book: uuid.UUID) -> list[str]:
    return sorted(row[0] for row in conn.execute(
        sql.SQL("SELECT to_jsonb(t)::text FROM {} AS t WHERE owner_id=%s AND book_id=%s")
        .format(sql.Identifier(table)), (owner, book)).fetchall())


def everything_of(dsn: str, owner: uuid.UUID) -> dict[str, list[str]]:
    """Every row of one owner in every table with an owner_id, read past row-level security."""
    with psycopg.connect(dsn) as conn:
        tables = [row[0] for row in conn.execute(
            """SELECT table_name FROM information_schema.columns
               WHERE table_schema='public' AND column_name='owner_id' ORDER BY 1""").fetchall()]
        return {table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT to_jsonb(t)::text FROM {} AS t WHERE owner_id=%s")
            .format(sql.Identifier(table)), (owner,)).fetchall()) for table in tables}


def a_cost(dsn: str, owner: uuid.UUID, book: str) -> None:
    """A money record of the book, made up, as the database owner. (Until ADR 0044 step 7 this
    also made a row in every table of a book's story memory under the reader; a reader owns none
    since step 5, and their session can make none since step 7.)"""
    with psycopg.connect(dsn) as conn:
        incarnation = conn.execute("SELECT incarnation FROM books WHERE owner_id=%s AND id=%s",
                                   (owner, uuid.UUID(book))).fetchone()[0]
        scope = (owner, uuid.UUID(book), incarnation)
        job = conn.execute("SELECT id FROM jobs WHERE owner_id=%s AND book_id=%s LIMIT 1",
                           scope[:2]).fetchone()[0]
        conn.execute("INSERT INTO cost_ledger (owner_id,id,book_id,book_incarnation,job_id,phase,"
                     "provider,model,input_tokens,output_tokens,usd,idempotency_key,purpose) "
                     "VALUES (%s,%s,%s,%s,%s,'extraction','made-up','model',10,2,0.01,%s,"
                     "'conversation')", (owner, uuid.uuid4(), *scope[1:], job,
                                         f"made-up-{uuid.uuid4().hex}"))


def a_full_book(client, tokens, app, readers, label: str, monkeypatch=None) -> tuple:
    """A reader's book with something in every corner: read map, place, preferences, a highlight,
    a note on it, a bookmark, a conversation, chapter notes, notes Mothli keeps (tied to it,
    about the reader and said while reading it, about the reader from elsewhere), the home chat
    before and while reading it, and its story memory and costs. Returns (reader, book)."""
    who = reader((client, tokens, app), readers, label)
    said(app, who.owner, None, HOME_BEFORE)               # nothing read anywhere yet
    book = upload(client, tokens, label, lamp_book())
    who.mark(book, [1])
    who.read(book, 2, 1, 3, skimmed=True)
    catalog, source = adapters(app, who.owner)
    _state, record, _manifest, chapters = source_state(book, catalog, app.state.settings, source)
    catalog.set_chapter_notes(book, 1, source_hash=text_hash(chapters[0]["text"]), version=VERSION,
                              sentences=[NOTE], dropped=0)
    read = {book: [record["incarnation"], {1: [[0, len(chapters[0]["text"])]]}]}
    tied = kept([("taste", TIED)])
    tied[0].update(tied=True, book_id=book, drew_on=read)
    leant = kept([("about", LEANT)])
    leant[0].update(drew_on=read)
    HostedCompanionCatalog(app.state.companion_dsn, OwnerId(who.owner)).keep_recollections(
        tied + leant + kept([("about", UNTIED)]), cap=STORE_CAP)
    said(app, who.owner, book, TALK)
    said(app, who.owner, None, HOME_DURING)               # leans on the book's read text
    anchor = {"cfi": "epubcfi(/6/2!/4/2,/1:0,/1:4)", "atom": 1}
    highlight = who.call("POST", f"/api/books/{book}/highlights", {
        "anchor": anchor, "color": "yellow", "selected_text": "Mara"})
    assert highlight.status_code == 201, highlight.text
    assert who.call("POST", f"/api/books/{book}/annotations", {
        "anchor": anchor, "body": "Mara again.", "highlight_id": highlight.json()["id"]
    }).status_code == 201
    assert who.call("POST", f"/api/books/{book}/bookmarks", {
        "anchor": anchor, "label": "Lamps"}).status_code == 201
    assert who.call("PUT", f"/api/books/{book}/preferences", {
        "font_size": "book", "line_height": "comfortable", "measure": "balanced",
        "theme": "paper", "margins": "balanced", "typeface": "publisher"}).status_code == 200
    return who, book


def test_every_table_about_a_book_is_erased_or_kept_for_a_reason(database) -> None:
    """The break it catches: a table a later migration adds with an owner and a book that removing
    a book neither erases (directly or through a cascade from what it erases) nor keeps for a
    stated reason; or a kept table the tenant role could delete from. ADR 0044 step 7: or a
    story memory table the tenant role holds anything on, or that removing a book erases from
    (it would be the edition every other reader of the file is handed)."""
    with psycopg.connect(database) as conn:
        tables = set(owner_book_tables(conn))
        erased = set(ERASED_WITH_THE_BOOK) | cascades_from(conn, ERASED_WITH_THE_BOOK)
    assert set(KEPT) <= tables and set(NOT_A_READERS) <= tables
    left = tables - set(KEPT) - set(NOT_A_READERS)
    assert left <= erased, sorted(left - erased)
    assert not (set(KEPT) | set(NOT_A_READERS)) & erased
    # The tenant role holds exactly the DELETE it needs for this, none on what is kept, and
    # nothing at all on story memory.
    for table in ERASED_WITH_THE_BOOK:
        assert "DELETE" in TENANT_TABLE_PRIVILEGES[table], table
    for table in (*KEPT, "books", "audit_events"):
        assert "DELETE" not in TENANT_TABLE_PRIVILEGES.get(table, frozenset()), table
    for table in NOT_A_READERS:
        assert table not in TENANT_TABLE_PRIVILEGES, table


def test_removing_a_book_erases_its_reading_keeps_the_money_and_never_brings_it_back(
        notes_app, readers, database, monkeypatch):
    """The break it catches: a row about the removed book's reading left in any table with an
    owner and a book (found from the catalogue), the home chat's messages that leaned on it or the
    message a kept note came from left behind; a money record, cancelled job or audit line erased;
    a note about the reader that is not about the book lost; its file left in storage; or a
    removed book's chapter notes, what Mothli remembers from it, or its conversation given to a
    later reply (the same book added again, or the home chat)."""
    client, tokens, app, objects = notes_app
    a, lamps = a_full_book(client, tokens, app, readers, "a")
    upload(client, tokens, "a", cart_book())          # the shelf is not empty afterwards
    a_cost(database, a.owner, lamps)
    book_id = uuid.UUID(lamps)

    # Before: each of them reaches a reply (so their absence later means something).
    models = HomeModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a.ask(lamps, QUESTION)
    before = book_prompt(models)
    assert NOTE in before and TIED in before and TALK in before and UNTIED in before
    a.ask_home(QUESTION)
    assert TIED in book_prompt(models)
    stored = rows(database, "SELECT storage_key FROM source_objects WHERE owner_id=%s AND book_id=%s",
                  a.owner, book_id)[0]["storage_key"]
    stored_file = objects / a.owner.hex / f"{stored}.epub.enc"
    assert stored_file.exists()
    with psycopg.connect(database) as conn:
        tables = owner_book_tables(conn)
        held = {table: about(conn, table, a.owner, book_id) for table in tables}
    # Every table that is erased holds something of this book first, the kept ones included
    # where this book has them, so "nothing afterwards" is a real change.
    empty = {table for table, found in held.items() if not found}
    assert empty <= {"cost_reservations", *NOT_A_READERS}, sorted(empty)
    costs = held["cost_ledger"]

    assert a.call("DELETE", f"/api/books/{lamps}").status_code == 204

    # What goes: the book's file; every row about its reading, in every table with an owner and a
    # book that is not kept for a reason.
    assert not stored_file.exists()
    with psycopg.connect(database) as conn:
        for table in tables:
            if table not in KEPT:
                assert about(conn, table, a.owner, book_id) == [], table
        assert about(conn, "cost_ledger", a.owner, book_id) == costs
        jobs = conn.execute("SELECT state, cancellation_requested_at IS NOT NULL FROM jobs "
                            "WHERE owner_id=%s AND book_id=%s", (a.owner, book_id)).fetchall()
        assert jobs and all(cancelled for _state, cancelled in jobs)
        # The book's row stays as a bare id for those records, with nothing of what it was.
        assert conn.execute(
            "SELECT title, author, source_kind, source_id, file_hash, split_hash, "
            "deleted_at IS NOT NULL "
            "FROM books WHERE owner_id=%s AND id=%s", (a.owner, book_id)).fetchone() == (
            "", None, None, None, None, None, True)
        assert ("book.delete", "succeeded") in conn.execute(
            "SELECT action, result FROM audit_events WHERE owner_id=%s AND target_id=%s",
            (a.owner, book_id)).fetchall()
        # The home chat: what was said before any reading stays; what leaned on the book goes.
        home = [row[0] for row in conn.execute(
            "SELECT content FROM conversation_turns WHERE owner_id=%s AND book_id IS NULL "
            "AND role='user' ORDER BY turn_id", (a.owner,)).fetchall()]
        assert HOME_BEFORE in home and HOME_DURING not in home
        # Notes about the reader stay; the one said while reading it loses only its message.
        notes = {row[0]: row[1:] for row in conn.execute(
            "SELECT text, said, drew_on FROM recollections WHERE owner_id=%s", (a.owner,))}
        assert set(notes) >= {UNTIED, LEANT} and TIED not in notes
        assert notes[LEANT] == ("", None) and notes[UNTIED][0] == UNTIED
        assert not conn.execute("SELECT 1 FROM recollection_cursors WHERE owner_id=%s "
                                "AND conversation=%s", (a.owner, lamps)).fetchall()
    assert lamps not in [book["book_id"] for book in a.call("GET", "/api/books").json()]
    for path in ("", "/epub", "/manifest", "/position", "/marks", "/read-map", "/conversation"):
        assert a.call("GET", f"/api/books/{lamps}{path}").status_code == 404, path
    assert {note["text"] for note in a.notes_listed()["recollections"]} == {UNTIED, LEANT}

    # The home chat: nothing of the removed book.
    a.ask_home(QUESTION)
    home = book_prompt(models)
    for gone in (NOTE, TIED, TALK, HOME_DURING):
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


def test_removing_a_book_moves_nothing_of_another_reader_with_the_same_book(
        notes_app, readers, database):
    """The break it catches: removal predicates that reach another reader's rows, their own copy
    of the very same file included (compared row for row before and after, in every table with an
    owner), or a second reader able to remove the first reader's book."""
    client, tokens, app, _objects = notes_app
    a, lamps_a = a_full_book(client, tokens, app, readers, "a")
    b, lamps_b = a_full_book(client, tokens, app, readers, "b")
    a_cost(database, a.owner, lamps_a)
    a_cost(database, b.owner, lamps_b)
    a_before = everything_of(database, a.owner)

    # B cannot remove A's book: the same answer as a book that does not exist, and nothing of A's
    # moves.
    hidden = b.call("DELETE", f"/api/books/{lamps_a}")
    absent = b.call("DELETE", f"/api/books/{uuid.uuid4()}")
    assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
    assert hidden.status_code == 404
    assert everything_of(database, a.owner) == a_before

    b_before = everything_of(database, b.owner)
    assert a.call("DELETE", f"/api/books/{lamps_a}").status_code == 204
    assert everything_of(database, b.owner) == b_before
    assert not everything_of(database, a.owner)["chapter_notes"]
    assert b.call("GET", f"/api/books/{lamps_b}/marks").json()["marks"]
    assert TALK in [turn["content"] for turn in b.call(
        "GET", f"/api/books/{lamps_b}/conversation").json()["turns"]]


def test_a_removal_cut_off_between_storage_and_database_is_safe_to_ask_again(
        notes_app, readers, database, monkeypatch):
    """The break it catches: a removal whose database step fails after the file is gone that
    leaves the book half erased, or that cannot be finished by asking again."""
    client, tokens, app, objects = notes_app
    a, lamps = a_full_book(client, tokens, app, readers, "a")
    book_id = uuid.UUID(lamps)
    with psycopg.connect(database) as conn:
        tables = [t for t in owner_book_tables(conn) if t not in KEPT]
        before = {t: about(conn, t, a.owner, book_id) for t in tables}
    repository = app.state.tenant_repository
    real = type(repository).remove_book

    async def cut_off(self, owner_id, book):
        raise psycopg.OperationalError("made-up: the database went away")

    monkeypatch.setattr(type(repository), "remove_book", cut_off)
    with pytest.raises(psycopg.OperationalError):
        a.call("DELETE", f"/api/books/{lamps}")
    stored = rows(database, "SELECT storage_key FROM source_objects WHERE owner_id=%s AND book_id=%s",
                  a.owner, book_id)[0]["storage_key"]
    assert not (objects / a.owner.hex / f"{stored}.epub.enc").exists()
    with psycopg.connect(database) as conn:
        assert {t: about(conn, t, a.owner, book_id) for t in tables} == before

    monkeypatch.setattr(type(repository), "remove_book", real)
    assert a.call("DELETE", f"/api/books/{lamps}").status_code == 204
    with psycopg.connect(database) as conn:
        for table in tables:
            assert about(conn, table, a.owner, book_id) == [], table
    assert a.call("DELETE", f"/api/books/{lamps}").status_code == 404
