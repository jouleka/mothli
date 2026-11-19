"""No reader corrects notes, and a reader's session reaches story memory only through the door
(ADR 0044 section 7, section 4 point 3, step 7), against real PostgreSQL.

A book's notes are its edition's: rows of the notes account, made by readers' runs on the worker
role and handed to each reader by ``story_memory_door`` up to that reader's own place. After step
7 the tenant role holds no privilege on any story memory table, so even a query that forgot its
owner predicate, or a new route that read a table directly, is refused by the database before row
security is reached. The three routes that read or changed a reader's own memory (``GET /memory``
and ``GET`` and ``POST /memory-corrections``) are gone, and answer for every book exactly as a
route that never existed does. Removing a book, adding the same file again, taking a copy of one's
data and erasing an account work without the tables. Stand-in writer and checker only: nothing is
called and nothing paid.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from psycopg import conninfo, errors, sql

from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_account
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES, purge_grants
from app.hosted.tenant.repository import PostgresTenantRepository
from tests.hosted import test_story_memory_door_postgres as door_gate
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_account_export_postgres import copy_of
from tests.hosted.test_companion_stream_postgres import restore_caps  # noqa: F401  (autouse)
from tests.hosted.test_story_memory_door_postgres import (
    LONG,
    NAMES,
    add,
    built_edition,
    door_bytes,
    everything_of,
    get,
    reader,
    through,
    upload,
)

pytestmark = pytest.mark.postgres

admin_dsn = door_gate.admin_dsn
database = door_gate.database
tenant_dsn = door_gate.tenant_dsn
worker_dsn = door_gate.worker_dsn
people = door_gate.people
source = door_gate.source
shelf = door_gate.shelf

# Every table of a book's story memory, named here rather than read from the code, so that a table
# dropped from the code's own list is still checked. The door reads the first nine.
STORY_MEMORY = ("chapters", "ingested_chapters", "chapter_summaries", "entities", "aliases",
                "entity_state", "edges", "events", "themes", "event_participants",
                "entity_corrections", "chunks", "chunk_embeddings")
EVERY_PRIVILEGE = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
# The routes step 7 removed, each as a reader's app would call it.
GONE = (("GET", "memory", None), ("GET", "memory-corrections", None),
        ("GET", "memory-corrections?bookmark=2", None), ("POST", "memory-corrections", "body"))


def tenant_role(tenant_dsn: str) -> str:
    return conninfo.conninfo_to_dict(tenant_dsn)["user"]


def held(database: str, role: str, table: str) -> set[str]:
    with psycopg.connect(database) as conn:
        return {privilege for privilege in EVERY_PRIVILEGE if conn.execute(
            "SELECT has_table_privilege(%s,%s,%s)", (role, f"public.{table}", privilege)
        ).fetchone()[0]}


# ---- 1: the grants -------------------------------------------------------------------------------

def test_the_tenant_role_holds_no_privilege_on_any_story_memory_table(database, tenant_dsn):
    """The break it catches: a tenant role left with a privilege on a story memory table (the
    reviewed list, the role as granted, or the startup check that would not notice one granted
    back), so that a query in the web process could read or change an edition's rows, or a
    reader's own, without the door; or a door the role can no longer execute."""
    role = tenant_role(tenant_dsn)
    for table in STORY_MEMORY:
        assert table not in TENANT_TABLE_PRIVILEGES, table
        assert held(database, role, table) == set(), table
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT has_function_privilege(%s,'story_memory_door(uuid)',"
                            "'EXECUTE')", (role,)).fetchone()[0]
    check = PostgresTenantRepository(tenant_dsn)
    asyncio.run(check.check_runtime_role())
    for table, privilege in (("chapters", "SELECT"), ("entities", "UPDATE"),
                             ("entity_corrections", "INSERT"), ("chapters", "DELETE")):
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(privilege), sql.Identifier(table), sql.Identifier(role)))
        try:
            with pytest.raises(AuthConfigurationError, match="allow-list"):
                asyncio.run(check.check_runtime_role())
        finally:
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute(sql.SQL("REVOKE {} ON {} FROM {}").format(
                    sql.SQL(privilege), sql.Identifier(table), sql.Identifier(role)))
    asyncio.run(check.check_runtime_role())


# ---- 2: the database refuses a reader's session every row ---------------------------------------

def refused(tenant_dsn: str, as_owner, statement: sql.Composable, params=()) -> None:
    with psycopg.connect(tenant_dsn) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(as_owner),))
                conn.execute(statement, params)


def test_a_readers_session_is_refused_every_story_memory_row_their_own_or_an_editions(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: row security as the only thing between a reader's session and the
    notes, so a query that forgot its owner predicate, or that set the notes account as its
    owner, reads an edition past the reader's place; or a reader's session that can still write
    a memory row (their own book's, as a correction did, or the edition's). Every statement is
    refused by the database, the rows stay as they were, and the door still hands the reader
    exactly their part."""
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    a = reader(shelf, "r5a")
    book = add(a)
    a.mark(book, [1, 2, 3, 4, 5])
    with psycopg.connect(database) as conn:
        [(edition,)] = conn.execute("SELECT id FROM books WHERE owner_id=%s AND deleted_at IS NULL"
                                    " AND edition_of IS NOT NULL", (account,)).fetchall()
        # A row of the reader's own, as memory a reader once owned would be (none is made since
        # ADR 0044 step 5): the database refuses it too.
        [(incarnation,)] = conn.execute("SELECT incarnation FROM books WHERE owner_id=%s AND id=%s",
                                        (a.owner, book)).fetchall()
        conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,id,chapter_key,"
                     "revealed_at,content_hash) VALUES (%s,%s,%s,%s,'own-1',1,%s)",
                     (a.owner, book, incarnation, uuid.uuid4(), "a" * 64))
    before, own = everything_of(database, account), everything_of(database, a.owner)

    for as_owner, (owner, book_id) in ((a.owner, (a.owner, book)), (a.owner, (account, edition)),
                                       (account, (account, edition))):
        for table in STORY_MEMORY:
            name = sql.Identifier(table)
            where = sql.SQL("owner_id=%s AND book_id=%s")
            refused(tenant_dsn, as_owner, sql.SQL("SELECT * FROM {}").format(name))
            refused(tenant_dsn, as_owner,
                    sql.SQL("SELECT count(*) FROM {} WHERE {}").format(name, where),
                    (owner, book_id))
            refused(tenant_dsn, as_owner, sql.SQL("INSERT INTO {} DEFAULT VALUES").format(name))
            refused(tenant_dsn, as_owner,
                    sql.SQL("UPDATE {} SET owner_id=owner_id WHERE {}").format(name, where),
                    (owner, book_id))
            refused(tenant_dsn, as_owner,
                    sql.SQL("DELETE FROM {} WHERE {}").format(name, where), (owner, book_id))
            refused(tenant_dsn, as_owner, sql.SQL("TRUNCATE {}").format(name))
    assert everything_of(database, account) == before
    assert everything_of(database, a.owner) == own

    # The door, the only way in, still works for that role, and hands only the reader's part.
    assert through(tenant_dsn, a.owner, book) == 5
    graph = get(a, f"/api/books/{book}/graph?bookmark=99")
    assert graph.status_code == 200
    assert {row["canonical_name"] for row in graph.json()["characters"]} == set(NAMES[:5])


# ---- 3: the three routes are gone -----------------------------------------------------------------

def test_the_memory_and_correction_routes_answer_as_an_unknown_route_for_every_book(
        shelf, tenant_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: a reader's own memory read, or a correction, still reachable in
    hosted mode (a correction to shared notes would change what every other reader of the file
    is shown); or a route that answers differently for a book whose file has an edition, one
    whose file has none, another reader's book and no book at all, which would tell a reader
    whether their file is shared. Each answers exactly as a route that never existed, and
    nothing is written or queued."""
    account, models, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    a, b = reader(shelf, "r5a"), reader(shelf, "r2a")
    shared = add(a)
    a.mark(shared, [1, 2, 3, 4, 5])
    alone = upload(shelf, a, door_gate.another_file_of_the_same_title())
    unread = upload(shelf, a, LONG)
    foreign = add(b)
    edition, calls = everything_of(database, account), list(models.calls)
    mine = everything_of(database, a.owner)
    body = {"source_entity_id": str(uuid.uuid4()), "canonical_name": "Someone Else",
            "reason": "a typo", "bookmark": 2}
    for book in (shared, alone, unread, foreign, str(uuid.uuid4())):
        for method, path, payload in GONE:
            gone = a.call(method, f"/api/books/{book}/{path}", body if payload else None)
            never = a.call(method, f"/api/books/{book}/no-such-route", body if payload else None)
            assert (gone.status_code, gone.json()) == (never.status_code, never.json()) \
                == (404, {"detail": "Not Found"}), (book, method, path)
    assert everything_of(database, account) == edition and models.calls == calls
    assert everything_of(database, a.owner) == mine


# ---- 4: removal, the same file again, the copy of one's data, erasure ------------------------------

def test_removal_the_same_file_again_the_copy_and_erasure_work_without_the_memory_tables(
        shelf, tenant_dsn, admin_dsn, database, worker_dsn, monkeypatch):
    """The break it catches: removing a book, adding the very same file again, taking a copy of
    one's data or erasing an account that still needs the tenant role to read or delete a story
    memory row (so it fails once the role holds none), that reaches the edition (every other
    reader of the file would lose its notes), or that leaves a removed book's link to the
    edition behind, so the same file added again starts past place 0."""
    account, *_ = built_edition(shelf, worker_dsn, monkeypatch)
    edition = everything_of(database, account)
    a, b = reader(shelf, "r5a"), reader(shelf, "r2a")
    book = upload(shelf, a, LONG)
    a.mark(book, [1, 2, 3])
    other = add(b)
    b.mark(other, [1, 2])
    assert through(tenant_dsn, a.owner, book) == 3

    # The copy of one's data: the cast and the story at the reader's place, through the door.
    files = copy_of(a.call("GET", "/api/account/export"))
    copied = "\n".join(files.values())
    assert all(name in copied for name in NAMES[:3])
    assert not [name for name in NAMES[3:] if name in copied]

    # The position routes, which counted the reader's own receipts, still answer and move.
    position = get(a, f"/api/books/{book}/position")
    assert position.status_code == 200 and position.json()["bookmark"] == 3

    # Removing the book: the reader's rows go, the edition stays, the other reader is untouched.
    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    assert everything_of(database, account) == edition
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT file_hash,split_hash FROM books WHERE owner_id=%s AND id=%s",
                            (a.owner, book)).fetchone() == (None, None)
        for table in ("reading_state", "read_ranges", "read_position", "source_objects",
                      "book_search_documents"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id=%s",
                                (a.owner, book)).fetchone()[0] == 0, table
    assert through(tenant_dsn, b.owner, other) == 2

    # The same file again: a new book, at place 0 (the door's empty row, byte for byte the same
    # as for a file nobody built), then handed the edition as the reader reads on.
    again = upload(shelf, a, LONG)
    assert again != book
    alone = upload(shelf, a, door_gate.another_file_of_the_same_title())
    assert door_bytes(tenant_dsn, a.owner, again) == door_bytes(tenant_dsn, a.owner, alone)
    a.mark(again, [1, 2])
    assert through(tenant_dsn, a.owner, again) == 2
    assert everything_of(database, account) == edition

    # Erasing the account: everything of the reader goes, the edition and the other reader stay.
    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
        conn.execute("UPDATE users SET status='deleting',erase_after=now()-interval '1 day'"
                     " WHERE id=%s", (a.owner,))
    with _role(admin_dsn, database, bypass=True, tables=tables) as purge_dsn:
        assert purge_account(PostgresAccountPurge(purge_dsn), shelf.app.state.object_storage,
                             a.owner)
    assert all(rows == [] for rows in everything_of(database, a.owner).values())
    assert everything_of(database, account) == edition
    assert through(tenant_dsn, b.owner, other) == 2
