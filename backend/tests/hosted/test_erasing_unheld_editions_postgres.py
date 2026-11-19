"""Erasing an edition nobody holds (ADR 0044 section 6 and step 8), against real PostgreSQL.

An edition is the one shared set of story notes for one exact book file: a ``books`` row of the
notes account, marked by ``edition_of``. A holder of the file is a reader with a live ``books``
row carrying the same two fingerprints, whose account is not pending deletion (or being erased).
The daily purge marks an edition with no holder with the time it first found it so ("no holder
since"), clears the mark when a holder is back, and erases the edition once it has had no holder
for 30 days: its row becomes a bare id, every story-memory row under it goes by the chapter
cascade, and so does any stored file an edition made by the removed operator's command still
has. The notes account itself, its limits and its ledger stay. Proven here, each written before
the code it guards:

- removing a book and erasing an account leave the edition while another reader holds the file,
  and a mark found on a held edition is cleared, never acted on;
- an account pending deletion, or being erased, is no holder; one that keeps its account is a
  holder again and clears the mark;
- the last holder gone: the edition is marked, is still there and served to nobody at 29 days,
  and is erased at 30, its rows, its stored file and the file's record gone, the ledger kept;
- a holder back before the 30 days clears the mark and is handed what was there up to their own
  place and no more; when they leave, the 30 days start again;
- races, with real concurrent transactions: a file uploaded and read while the purge erases its
  edition never lands notes in the erased edition, never leaves it half erased, never makes two
  live editions and never fails the reader's run for good (the run asks again and builds a fresh
  edition from chapter 1); a run building, or publishing, while the purge decides keeps the
  edition whole; a file uploaded while the erasure waits for the edition's lock keeps it;
- after the erasure, the door and every story screen answer a reader of the file exactly as for
  a file nobody built, and the file's next reader pays for its notes again;
- the mark and the erasure store nothing that names a reader, a reader's book or a file name;
- the purge run twice in a row changes nothing the second time.

Stand-in writer, checker and Jev only: nothing is called and nothing paid.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import conninfo, sql
from psycopg.rows import dict_row

from app.hosted import purge as purging
from app.hosted.auth.repository import PostgresAuthRepository
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_account
from app.hosted.storage import EPUB_MEDIA_TYPE, new_source_ref
from app.hosted.tenant.grants import (
    PURGE_OTHER_TABLE_PRIVILEGES,
    STORY_MEMORY_TABLES,
    TENANT_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
    purge_grants,
)
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted import test_story_memory_jobs_postgres as memory_gate
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    restore_caps,
)
from tests.hosted.test_story_memory_jobs_postgres import (
    OTHER_FILE,
    Lamps,
    built_in,
    drain,
    jobs_of,
    lamps,
    opened,
    rows,
    slot,
    worker,
)
from tests.hosted.test_story_memory_postgres import (
    MEMORY_TABLES,
    book_memory,
    edition_of,
    notes_account,
    settled_once,
)
from tests.hosted.test_story_memory_same_screens_postgres import seen
from tests.parity.test_story_memory_parity import extraction_client

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
worker_dsn = jobs_gate.worker_dsn
memory_app = memory_gate.memory_app

THIRD_FILE = "The Lamp Book, A Third Printing"


@pytest.fixture()
def readers(database: str) -> dict:
    """Four readers (``memory_app`` signs in every one of them)."""
    owners = {label: uuid.uuid4() for label in ("a", "b", "c", "d")}
    with psycopg.connect(database) as conn:
        conn.cursor().executemany(
            "INSERT INTO users (id,display_name) VALUES (%s,%s)",
            [(owner, f"Reader {label.upper()}") for label, owner in owners.items()])
    return owners


@pytest.fixture()
def purge(admin_dsn: str, database: str):
    """The purge, under a role holding exactly what the deployment grants it, checked."""
    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
    with _role(admin_dsn, database, bypass=True, tables=tables) as dsn:
        repository = PostgresAccountPurge(dsn)
        repository.check_runtime_role()
        yield repository


# ---- helpers -------------------------------------------------------------------------------------

def editions(memory_app, purge):
    """One daily run of the editions' part of the purge."""
    return purging.purge_unheld_editions(purge, memory_app[2].state.object_storage)


@pytest.fixture()
def build(memory_app, readers, database, worker_dsn, monkeypatch):
    """``build(label, marked=..., title=...)``: reader ``label`` adds the file, reads its first
    page and marks ``marked`` read; their runs build the edition up to one chapter past that.
    Returns (reader, book, edition scope)."""
    _client, _tokens, app = memory_app

    def built(label: str, *, marked=(), title: str = "The Lamp Book"):
        someone, book = opened(memory_app, readers, label, title)
        if marked:
            someone.mark(book, list(marked))
        extraction = extraction_client()
        Lamps(extraction, monkeypatch)
        drain(worker(app, extraction, worker_dsn, f"builder-{label}"), 20)
        return someone, book, edition_of(database, someone.owner, book)

    return built


def remove(someone, book) -> None:
    assert someone.call("DELETE", f"/api/books/{book}").status_code == 204


def mark_of(dsn: str, scope):
    [row] = rows(dsn, "SELECT no_holder_since FROM books WHERE owner_id=%s AND id=%s", *scope)
    return row["no_holder_since"]


def age(dsn: str, scope, days: int) -> None:
    """The mark set ``days`` days ago, as if the purge had first found no holder then."""
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE books SET no_holder_since=now()-%s*interval '1 day'"
                     " WHERE owner_id=%s AND id=%s", (days, *scope))


def live_editions(dsn: str, scope_or_hashes) -> list[dict]:
    """Every live marked edition of one file (given an edition's scope or the two hashes)."""
    if len(scope_or_hashes) == 2 and isinstance(scope_or_hashes[0], uuid.UUID):
        [found] = rows(dsn, "SELECT file_hash,split_hash FROM books WHERE owner_id=%s AND id=%s",
                       *scope_or_hashes)
        hashes = (found["file_hash"], found["split_hash"])
    else:
        hashes = scope_or_hashes
    return rows(dsn, "SELECT * FROM books WHERE edition_of IS NOT NULL AND deleted_at IS NULL"
                " AND file_hash=%s AND split_hash=%s", *hashes)


def hashes_of(dsn: str, scope) -> tuple[str, str]:
    [found] = rows(dsn, "SELECT file_hash,split_hash FROM books WHERE owner_id=%s AND id=%s",
                   *scope)
    return found["file_hash"], found["split_hash"]


def edition_rows(dsn: str, scope) -> dict:
    """The edition's row and every row under it: story memory, and its stored file's record."""
    held = book_memory(dsn, scope)
    with psycopg.connect(dsn) as conn:
        held["books"] = [conn.execute("SELECT to_jsonb(b) - 'no_holder_since' FROM books AS b"
                                      " WHERE owner_id=%s AND id=%s", scope).fetchone()[0]]
        held["source_objects"] = sorted(row[0] for row in conn.execute(
            "SELECT s::text FROM source_objects AS s WHERE owner_id=%s AND book_id=%s",
            scope).fetchall())
    return held


def door(tenant: str, owner, book) -> list[dict]:
    with psycopg.connect(tenant, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute("SELECT * FROM story_memory_door(%s)", (book,)).fetchall()


def handed(tenant: str, owner, book) -> tuple[list[int], list[str]]:
    """The chapters the door hands this reader as complete, and the people's names."""
    [row] = door(tenant, owner, book)
    return (sorted(item["ordinal"] for item in row["completed"]),
            sorted(person["name"] for person in row["entities"]))


def notes_kept(dsn: str) -> dict:
    """What the erasure of an edition must leave of the notes account: the account, its limits,
    what the notes cost and its jobs."""
    account = notes_account(dsn)
    with psycopg.connect(dsn) as conn:
        return {table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT t::text FROM {} AS t WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(column)), (account,)).fetchall())
            for table, column in (("users", "id"), ("notes_account", "account_id"),
                                  ("owner_limits", "owner_id"), ("cost_ledger", "owner_id"),
                                  ("cost_reservations", "owner_id"), ("jobs", "owner_id"))}


def everything(dsn: str) -> dict[str, list[str]]:
    """Every row of every table, read past row security."""
    with psycopg.connect(dsn) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT relname FROM pg_class AS c JOIN pg_namespace AS n ON n.oid=c.relnamespace"
            " WHERE n.nspname='public' AND c.relkind='r' ORDER BY 1").fetchall()]
        return {table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT t::text FROM {} AS t").format(sql.Identifier(table))).fetchall())
            for table in tables}


def stored_files(app) -> list[str]:
    root = app.state.object_storage._root
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def give_the_edition_a_file(app, dsn: str, scope):
    """What an edition the removed operator's command made still has (ADR 0044 step 6 "Built
    on" (3)): a stored copy of the file under the notes account, its record, and the title,
    author and free-library source the command gave it."""
    account, book = scope
    blob = lamps()
    ref = new_source_ref(OwnerId(account))
    receipt = app.state.object_storage.put(ref, blob, media_type=EPUB_MEDIA_TYPE,
                                           expected_sha256=hashlib.sha256(blob).hexdigest())
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO source_objects (owner_id,id,book_id,book_incarnation,storage_provider,"
            "storage_key,media_type,byte_size,sha256,encryption_key_id,verified_at)"
            " SELECT owner_id,%s,id,incarnation,%s,%s,%s,%s,%s,%s,now() FROM books"
            " WHERE owner_id=%s AND id=%s",
            (ref.object_id, receipt.provider, ref.object_id.hex, receipt.media_type,
             receipt.byte_size,
             receipt.sha256, receipt.encryption, account, book))
        conn.execute("UPDATE books SET title='Pride and Prejudice',author='Jane Austen',"
                     "source_kind='gutenberg:1342',source_id='1342' WHERE owner_id=%s AND id=%s",
                     scope)
    return ref


def user_of(dsn: str) -> str:
    return conninfo.conninfo_to_dict(dsn)["user"]


def waits(dsn: str, user: str, events: tuple[str, ...], timeout: float = 60) -> None:
    """Until a session of ``user`` waits on a lock of one of ``events`` (``advisory`` for the
    edition's lock; ``transactionid`` or ``tuple`` for a row)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with psycopg.connect(dsn) as conn:
            if conn.execute("SELECT count(*) FROM pg_stat_activity WHERE usename=%s"
                            " AND wait_event_type='Lock' AND wait_event=ANY(%s)",
                            (user, list(events))).fetchone()[0]:
                return
        time.sleep(0.05)
    raise AssertionError(f"{user} never waited on {events}")


class Background:
    """A call on its own thread, its result or its exception kept."""

    def __init__(self, call) -> None:
        self.result, self.error = None, None

        def run():
            try:
                self.result = call()
            except BaseException as exc:          # pragma: no cover - reported by join
                self.error = exc

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def join(self, timeout: float = 120):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "still running"
        if self.error is not None:
            raise self.error
        return self.result


# ---- 0: the privileges ----------------------------------------------------------------------------

def test_the_purge_role_gains_update_on_books_and_no_other_role_gains_anything() -> None:
    """The break it catches: the purge unable to turn an erased edition's row into a bare id or
    to mark it (or able to write anything else beyond its owner tables), the tenant role given
    any privilege on the story memory, or the worker given more on books than it had."""
    assert PURGE_OTHER_TABLE_PRIVILEGES == {
        "users": {"SELECT", "UPDATE"}, "audit_events": {"INSERT"}, "books": {"UPDATE"}}
    assert all(table not in TENANT_TABLE_PRIVILEGES for table in STORY_MEMORY_TABLES)
    assert TENANT_TABLE_PRIVILEGES["books"] == {"SELECT", "INSERT", "UPDATE"}
    assert WORKER_TABLE_PRIVILEGES["books"] == {"SELECT", "INSERT"}
    assert "notes_account" not in PURGE_OTHER_TABLE_PRIVILEGES


# ---- 1: held editions stay ------------------------------------------------------------------------

def test_removing_a_book_and_erasing_an_account_leave_the_edition_while_another_reader_holds_it(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """The break it catches: a reader's removal, or the purge erasing a reader's account, taken
    as the last holder gone while another reader still holds the file (the edition marked, or
    erased, under that reader); or a mark found on a held edition acted on rather than cleared."""
    _client, _tokens, app = memory_app
    a, book_a, scope = build("a", marked=[1])
    b, book_b = opened(memory_app, readers, "b")
    c, book_c = opened(memory_app, readers, "c")
    whole = edition_rows(database, scope)
    assert built_in(database, scope) == [(1, "body"), (2, "body")]

    remove(a, book_a)
    result = editions(memory_app, purge)
    assert (result.erased, result.unfinished, result.waiting) == (0, 0, 0)
    assert mark_of(database, scope) is None and edition_rows(database, scope) == whole

    with psycopg.connect(database) as conn:
        conn.execute("UPDATE users SET status='deleting',erase_after=now()-interval '1 day'"
                     " WHERE id=%s", (c.owner,))
    assert purge_account(purge, app.state.object_storage, c.owner)
    assert rows(database, "SELECT count(*) AS n FROM books WHERE owner_id=%s", c.owner)[0]["n"] == 0
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 0)
    assert mark_of(database, scope) is None and edition_rows(database, scope) == whole

    # A mark however old on an edition that is held is cleared, and nothing is erased.
    age(database, scope, 40)
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 0)
    assert mark_of(database, scope) is None and edition_rows(database, scope) == whole
    b.mark(book_b, [1])
    assert handed(tenant_dsn, b.owner, book_b) == ([1], ["Mara"])


def test_an_account_pending_deletion_is_no_holder_and_keeping_it_makes_it_one_again(
        memory_app, readers, database, worker_dsn, purge, monkeypatch, build):
    """The break it catches: a reader who asked to delete their account (signed out, refused
    everywhere, about to be erased) still counted as holding the file, so its notes would outlive
    every real reader; a reader who keeps their account left uncounted, so the notes of a book
    they hold would be erased under them; or an account being erased counted as a holder."""
    a, _book, scope = build("a")
    auth = PostgresAuthRepository(database)
    assert asyncio.run(auth.request_deletion(owner_id=a.owner, now=datetime.now(UTC)))
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 1)
    first = mark_of(database, scope)
    assert first is not None

    assert asyncio.run(auth.restore_account(owner_id=a.owner))
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 0)
    assert mark_of(database, scope) is None

    # Asked again and due: once the purge has begun erasing the account its live book still
    # holds nothing.
    assert asyncio.run(auth.request_deletion(owner_id=a.owner, now=datetime.now(UTC)))
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE users SET erase_after=now()-interval '1 day' WHERE id=%s",
                     (a.owner,))
    assert purge.begin(a.owner)
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 1)
    assert mark_of(database, scope) is not None
    assert built_in(database, scope) == [(1, "body")]


# ---- 2: the last holder gone ---------------------------------------------------------------------

def test_the_last_holder_gone_the_edition_is_marked_kept_at_29_days_and_erased_at_30(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """The break it catches: an edition erased the day its last holder left, or at 29 days, or
    never; served to anyone while it waits; erased in part (a story-memory row, the row itself
    with its fingerprints, title or source, or the stored file the operator's command left and
    its record, outliving it); or the erasure taking the notes account, its limits, what the
    notes cost or its jobs with it."""
    _client, _tokens, app = memory_app
    a, book_a, scope = build("a", marked=[1, 2])
    assert built_in(database, scope) == [(1, "body"), (2, "body"), (3, "body")]
    ref = give_the_edition_a_file(app, database, scope)
    assert app.state.object_storage.exists(ref)
    whole = edition_rows(database, scope)
    # The words of its names too (ADR 0044 step 10): they go with it, through its chapters.
    assert all(whole[table] for table in ("chapters", "ingested_chapters", "entities",
                                          "chapter_summaries", "source_objects", "name_words"))
    kept = notes_kept(database)
    assert kept["cost_ledger"] and kept["owner_limits"] and kept["users"]

    remove(a, book_a)
    assert door(tenant_dsn, a.owner, book_a) == []
    result = editions(memory_app, purge)
    assert (result.erased, result.unfinished, result.waiting) == (0, 0, 1)
    marked = mark_of(database, scope)
    with psycopg.connect(database) as conn:
        now = conn.execute("SELECT now()").fetchone()[0]
    assert marked is not None and abs((now - marked).total_seconds()) < 120
    assert edition_rows(database, scope) == whole and app.state.object_storage.exists(ref)
    # Nobody holds it: no live book of any reader carries its fingerprints.
    assert rows(database, "SELECT count(*) AS n FROM books WHERE file_hash=%s AND split_hash=%s"
                " AND deleted_at IS NULL AND owner_id<>%s", *hashes_of(database, scope),
                scope[0])[0]["n"] == 0

    # A second run the same day keeps the first mark.
    editions(memory_app, purge)
    assert mark_of(database, scope) == marked

    age(database, scope, 29)
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 1)
    assert edition_rows(database, scope) == whole and app.state.object_storage.exists(ref)

    age(database, scope, 30)
    result = editions(memory_app, purge)
    assert (result.erased, result.unfinished, result.waiting) == (1, 0, 0)
    assert book_memory(database, scope) == {table: [] for table in MEMORY_TABLES}
    [bare] = rows(database, "SELECT * FROM books WHERE owner_id=%s AND id=%s", *scope)
    assert bare["deleted_at"] is not None
    assert {key: bare[key] for key in ("title", "author", "source_kind", "source_id",
                                       "file_hash", "split_hash", "edition_of",
                                       "no_holder_since")} == {
        "title": "", "author": None, "source_kind": None, "source_id": None, "file_hash": None,
        "split_hash": None, "edition_of": None, "no_holder_since": None}
    assert rows(database, "SELECT * FROM source_objects WHERE owner_id=%s", scope[0]) == []
    assert not app.state.object_storage.exists(ref)
    assert notes_kept(database) == kept
    assert rows(database, "SELECT * FROM books WHERE edition_of IS NOT NULL") == []


def test_a_holder_back_within_30_days_clears_the_mark_gets_only_their_place_and_days_restart(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """The break it catches: a reader who brings the file back inside the 30 days losing its
    notes (or the mark kept, so they are erased under them), handed more than their own place
    because the notes were built further by someone gone, or the 30 days counted from the first
    mark when they leave again."""
    a, book_a, scope = build("a", marked=[1, 2])
    whole = edition_rows(database, scope)
    remove(a, book_a)
    editions(memory_app, purge)
    age(database, scope, 20)

    b, book_b = opened(memory_app, readers, "b")
    b.mark(book_b, [1])
    assert handed(tenant_dsn, b.owner, book_b) == ([1], ["Mara"])
    result = editions(memory_app, purge)
    assert (result.erased, result.waiting) == (0, 0)
    assert mark_of(database, scope) is None and edition_rows(database, scope) == whole
    assert handed(tenant_dsn, b.owner, book_b) == ([1], ["Mara"])

    remove(b, book_b)
    editions(memory_app, purge)
    again = mark_of(database, scope)
    with psycopg.connect(database) as conn:
        assert (conn.execute("SELECT now()").fetchone()[0] - again).total_seconds() < 120
    age(database, scope, 29)
    assert editions(memory_app, purge).erased == 0
    assert edition_rows(database, scope) == whole
    age(database, scope, 30)
    assert editions(memory_app, purge).erased == 1
    assert book_memory(database, scope) == {table: [] for table in MEMORY_TABLES}


# ---- 3: races -------------------------------------------------------------------------------------

def test_a_file_uploaded_and_built_while_the_purge_erases_its_edition_never_lands_in_it(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """Real concurrent transactions. The purge has decided to erase an edition nobody held for
    30 days, and is held just before it writes (another transaction holds the edition's row);
    meanwhile a reader uploads the same file, marks two chapters read, and their run builds
    chapter 3 into the edition it found and reaches the publish, which waits for the edition's
    lock the purge holds. The break it catches: chapter 3 published into the erased edition (or
    after a part of it was erased), the edition half erased, two live editions of the file, the
    reader's run failing (it must ask for its next run, which builds a fresh edition from
    chapter 1), or the reader handed anything past their place."""
    _client, _tokens, app = memory_app
    a, book_a, scope = build("a", marked=[1])
    assert built_in(database, scope) == [(1, "body"), (2, "body")]
    hashes = hashes_of(database, scope)
    remove(a, book_a)
    editions(memory_app, purge)
    age(database, scope, 31)

    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    work = worker(app, extraction, worker_dsn, "racer")
    holder = psycopg.connect(database)
    try:
        holder.execute("SELECT 1 FROM books WHERE owner_id=%s AND id=%s FOR NO KEY UPDATE",
                       scope)
        purging_now = Background(lambda: editions(memory_app, purge))
        waits(database, user_of(purge._dsn), ("transactionid", "tuple"))

        h, book_h = opened(memory_app, readers, "b")
        h.mark(book_h, [1, 2])
        running = Background(work.run_once)
        waits(database, user_of(worker_dsn), ("advisory",))
    finally:
        holder.rollback()
        holder.close()
    result = purging_now.join()
    assert running.join() is True
    assert (result.erased, result.unfinished) == (1, 0)

    # Erased whole; nothing landed in it.
    assert book_memory(database, scope) == {table: [] for table in MEMORY_TABLES}
    assert rows(database, "SELECT deleted_at IS NOT NULL AS gone FROM books WHERE owner_id=%s"
                " AND id=%s", *scope)[0]["gone"]
    assert live_editions(database, hashes) == []
    assert models.parts() == [3]
    # The run did not fail: it asked for its reader's next run.
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, h.owner)] == [
        ("succeeded", slot(3, scope, published=False)), ("pending", {})]

    # The next runs build a fresh edition of the file from its first chapter.
    assert drain(work) == 3
    [fresh] = live_editions(database, hashes)
    assert fresh["id"] != scope[1]
    assert built_in(database, (fresh["owner_id"], fresh["id"])) == [
        (1, "body"), (2, "body"), (3, "body")]
    assert models.parts() == [3, 1, 2, 3]
    assert all(row["state"] != "failed" for row in jobs_of(database, h.owner))
    assert handed(tenant_dsn, h.owner, book_h) == ([1, 2], ["Corvus", "Mara"])


def test_a_run_building_or_publishing_while_the_purge_decides_keeps_the_edition_whole(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """Real concurrent transactions. An edition whose mark says 31 days, and a reader of its file
    whose run is building chapter 3 into it. While the run is in the writer's call, the whole
    purge runs; then, while the run's publish holds the edition's lock with its rows written, the
    purge tries to erase the edition and waits. The break it catches: the purge deciding from a
    stale mark while a reader holds the file, erasing the edition under a publish, chapter 3
    lost or published into an erased edition, or the reader's run failing."""
    _client, _tokens, app = memory_app
    a, book_a, scope = build("a", marked=[1])
    remove(a, book_a)
    editions(memory_app, purge)
    age(database, scope, 31)

    in_writer, writer_go = threading.Event(), threading.Event()
    in_publish, publish_go = threading.Event(), threading.Event()

    def hold_the_writer(schema, part):
        if schema == "Extraction" and part == 3:
            in_writer.set()
            assert writer_go.wait(60)

    import app.hosted.jobs as hosted_jobs

    real_record = hosted_jobs.record_event

    def hold_the_publish(conn, **event):
        if event.get("action") == "memory.publish":
            in_publish.set()
            assert publish_go.wait(60)
        return real_record(conn, **event)

    monkeypatch.setattr(hosted_jobs, "record_event", hold_the_publish)
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, hook=hold_the_writer)
    work = worker(app, extraction, worker_dsn, "builder")
    h, book_h = opened(memory_app, readers, "b")
    h.mark(book_h, [1, 2])
    running = Background(work.run_once)
    try:
        assert in_writer.wait(60)
        # The purge decides while the run is building: the reader holds the file.
        result = editions(memory_app, purge)
        assert (result.erased, result.waiting) == (0, 0)
        assert mark_of(database, scope) is None

        # A stale decision: the mark says 31 days again, and the erasure is tried while the
        # publish holds the edition's lock with chapter 3's rows written, not yet committed.
        age(database, scope, 31)
        writer_go.set()
        assert in_publish.wait(60)
        erasing = Background(lambda: purge.erase_edition(*scope))
        waits(database, user_of(purge._dsn), ("advisory",))
    finally:
        writer_go.set()
        publish_go.set()
    assert running.join() is True
    assert erasing.join() is False
    assert built_in(database, scope) == [(1, "body"), (2, "body"), (3, "body")]
    assert mark_of(database, scope) is None
    assert [row["id"] for row in live_editions(database, scope)] == [scope[1]]
    assert models.parts() == [3]
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, h.owner)] == [
        ("succeeded", slot(3, scope))]
    assert handed(tenant_dsn, h.owner, book_h) == ([1, 2], ["Corvus", "Mara"])
    settled_once(database, scope[0])


def test_a_file_uploaded_while_the_erasure_waits_for_the_editions_lock_keeps_it(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """Real concurrent transactions. The purge has found an edition with no holder for 30 days
    and waits for the edition's lock (a publish holds it); a reader uploads the file meanwhile.
    The break it catches: the purge acting on what it saw before the lock, erasing the notes of
    a file a reader now holds, rather than deciding again once it has the lock."""
    _client, _tokens, _app = memory_app
    a, book_a, scope = build("a")
    whole = edition_rows(database, scope)
    remove(a, book_a)
    editions(memory_app, purge)
    age(database, scope, 31)

    lock = psycopg.connect(database)
    try:
        lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text || ':' || %s::text,"
                     " 48))", scope)
        purging_now = Background(lambda: editions(memory_app, purge))
        waits(database, user_of(purge._dsn), ("advisory",))
        h, book_h = opened(memory_app, readers, "b")
    finally:
        lock.rollback()
        lock.close()
    result = purging_now.join()
    assert (result.erased, result.unfinished, result.waiting) == (0, 0, 0)
    assert mark_of(database, scope) is None and edition_rows(database, scope) == whole
    h.mark(book_h, [1])
    assert handed(tenant_dsn, h.owner, book_h) == ([1], ["Mara"])


# ---- 4: after the erasure -------------------------------------------------------------------------

def test_after_the_erasure_a_reader_of_the_file_is_answered_as_for_a_file_nobody_built(
        memory_app, readers, database, tenant_dsn, worker_dsn, purge, monkeypatch, build):
    """The break it catches: anything of an erased edition reaching a reader who brings the file
    back (a row of the door, a person, a chapter, a different status or error on any story
    screen) where a reader of a file nobody built gets nothing; or the file's notes not built
    again for them."""
    _client, _tokens, app = memory_app
    a, book_a, scope = build("a", marked=[1])
    people = [row["id"] for row in rows(database, "SELECT id FROM entities WHERE owner_id=%s"
                                        " AND book_id=%s", *scope)]
    assert people
    remove(a, book_a)
    editions(memory_app, purge)
    age(database, scope, 30)
    assert editions(memory_app, purge).erased == 1

    c, book_c = opened(memory_app, readers, "c")
    d, book_d = opened(memory_app, readers, "d", OTHER_FILE)       # a file nobody built
    for someone, book in ((c, book_c), (d, book_d)):
        someone.mark(book, [1])
    assert door(tenant_dsn, c.owner, book_c) == door(tenant_dsn, d.owner, book_d)
    [row] = door(tenant_dsn, c.owner, book_c)
    assert row["chapters"] == [] and row["entities"] == [] and row["completed"] == []
    assert seen(c, book_c, people) == seen(d, book_d, people)

    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    drain(worker(app, extraction, worker_dsn, "again"), 20)
    fresh = edition_of(database, c.owner, book_c)
    assert fresh is not None and fresh[1] != scope[1]
    assert built_in(database, fresh) == [(1, "body"), (2, "body")]
    ledger = rows(database, "SELECT * FROM cost_ledger WHERE owner_id=%s AND book_id=%s", *fresh)
    assert ledger                                       # paid again, by the notes account
    assert handed(tenant_dsn, c.owner, book_c) == ([1], ["Mara"])


# ---- 5: nothing that names anyone -----------------------------------------------------------------

def test_the_mark_and_the_erasure_store_nothing_that_names_a_reader_their_book_or_a_file(
        memory_app, readers, database, worker_dsn, purge, monkeypatch, build):
    """The break it catches: the purge writing anything but the mark when it marks (a row, an
    audit line, a reader's id, their book's id, the title they typed, the file's name), or the
    erased edition's bare row, or anything else the erasure leaves, naming one; or the purge
    adding a row anywhere when it erases."""
    a, book_a, scope = build("a")
    b, book_b = opened(memory_app, readers, "b", OTHER_FILE)
    remove(a, book_a)
    names = [str(a.owner), str(book_a), str(b.owner), str(book_b), "The Lamp Book",
             "lamp.epub", "Ada Writer", "Reader A", "Reader B"]
    with psycopg.connect(database) as conn:
        before_row = conn.execute("SELECT to_jsonb(b) - 'no_holder_since' FROM books AS b"
                                  " WHERE owner_id=%s AND id=%s", scope).fetchone()[0]
    before = everything(database)
    editions(memory_app, purge)
    marked = everything(database)
    assert {table for table in before if before[table] != marked[table]} == {"books"}
    assert len(set(before["books"]) - set(marked["books"])) == 1
    assert len(set(marked["books"]) - set(before["books"])) == 1
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT to_jsonb(b) - 'no_holder_since' FROM books AS b"
                            " WHERE owner_id=%s AND id=%s", scope).fetchone()[0] == before_row
        [mark_row] = conn.execute("SELECT b::text FROM books AS b WHERE owner_id=%s AND id=%s",
                                  scope).fetchone()
    assert mark_of(database, scope) is not None
    assert not any(name in mark_row for name in names)

    hashes = hashes_of(database, scope)
    age(database, scope, 30)
    aged = everything(database)
    assert editions(memory_app, purge).erased == 1
    erased = everything(database)
    for table in aged:
        if table != "books":
            assert set(erased[table]) <= set(aged[table]), table
    assert erased["audit_events"] == aged["audit_events"]
    added = set(erased["books"]) - set(aged["books"])
    assert len(added) == 1 and len(set(aged["books"]) - set(erased["books"])) == 1
    [bare] = added
    assert str(scope[1]) in bare
    assert not any(name in bare for name in [*names, *hashes])
    for table, found in erased.items():
        for text in found:
            if str(scope[0]) in text:            # the notes account's rows
                assert not any(name in text for name in names[:2]), table


# ---- 6: twice in a row ----------------------------------------------------------------------------

def test_the_purge_run_twice_in_a_row_changes_nothing_the_second_time(
        memory_app, readers, database, worker_dsn, purge, monkeypatch, build):
    """The break it catches: a second run the same day moving a mark (so the 30 days would never
    end), erasing again, touching a held edition, or leaving work for later that the first run
    should have finished."""
    _client, _tokens, app = memory_app
    _a, _book_a, held = build("a")
    b, book_b, due = build("b", title=OTHER_FILE)
    c, book_c, fresh = build("c", title=THIRD_FILE)
    assert len({held[1], due[1], fresh[1]}) == 3
    ref = give_the_edition_a_file(app, database, due)
    remove(b, book_b)
    remove(c, book_c)
    editions(memory_app, purge)
    age(database, due, 30)

    first = editions(memory_app, purge)
    assert (first.erased, first.unfinished, first.waiting) == (1, 0, 1)
    assert not app.state.object_storage.exists(ref)
    once, files = everything(database), stored_files(app)
    second = editions(memory_app, purge)
    assert (second.erased, second.unfinished, second.waiting) == (0, 0, 1)
    assert everything(database) == once and stored_files(app) == files
    assert mark_of(database, held) is None and mark_of(database, fresh) is not None
