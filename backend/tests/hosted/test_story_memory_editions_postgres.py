"""Story memory built into the edition of the reader's file (ADR 0044 step 3), against real
PostgreSQL.

A reader's story-memory run (the same trigger, target and setting as before) builds the next
missing chapter of the **edition** of their file: the notes account's one marked book with the
same two fingerprints, made on its first chapter, holding no file and nothing that names a reader.
The chapter's text comes from that reader's own copy and nothing else; the edition's notes are read
by the worker role only below the chapter being built; readers are handed them only through the
door. Proven here, each written before the code it guards:

- two readers finishing one chapter at once make one build, one spend and one published chapter
  (the build slot on ``jobs``), and two first chapters at once make exactly one edition, which
  the database itself enforces (a marked row per live file at most), so the door never finds two;
- a run for a reader at chapter 3 of an edition others built to chapter 9 reads nothing of
  chapters 3 to 9, and the worker's statement holds the bound;
- a run reads only its own reader's copy of the file, and fails closed when that copy is not the
  edition's file split the same way, or an earlier chapter's text hash differs;
- a reader's session cannot select an edition's rows, before or after their own job built some;
- no edition row names a reader, their book, their title or their file name;
- the "two failures" and "three in a row" rules count the edition's chapters and every reader's
  failed runs of it;
- when the build slot frees after a lease expires: at the next claim, and not before;
- a reader's own rows from before (built while their own story memory was on) are left as they
  are, and the edition is built beside them.

Stand-in writer, checker and Jev only: nothing is called and nothing paid.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import Counter
from datetime import timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.hosted.companion.jobs import CompanionJobs, StoryMemoryQueue
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource, SourceParseCache
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import JobIdempotencyError, PostgresWorkerRepository
from app.hosted.purge import owner_tables
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.worker import DurableWorker
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    Reader,
    restore_caps,
)
from tests.hosted.test_library_story_memory_postgres import (
    LONG,
    NAMES,
    Writer,
    tpage,
)
from tests.hosted import test_story_memory_jobs_postgres as memory_gate
from tests.hosted.test_story_memory_jobs_postgres import (
    Lamps,
    built_in,
    drain,
    expire_lease,
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
    memory_of,
    no_memory_of_their_own,
    settled_once,
    spend_of,
)
from tests.parity.test_story_memory_parity import claimed, extraction_client

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
worker_dsn = jobs_gate.worker_dsn
memory_app = memory_gate.memory_app

# The tenant role's privileges on books and the memory tables as they were before this step
# (``grants.py`` on main at da1023d): the tenant role gains nothing here.
TENANT_BEFORE = {
    "books": {"SELECT", "INSERT", "UPDATE"}, "source_objects": {"SELECT", "INSERT", "UPDATE",
                                                                "DELETE"},
    "chapters": {"SELECT", "DELETE"}, "ingested_chapters": {"SELECT"},
    "chapter_summaries": {"SELECT"}, "entities": {"SELECT", "INSERT", "UPDATE"},
    "aliases": {"SELECT", "INSERT"}, "edges": {"SELECT", "INSERT"}, "events": {"SELECT"},
    "event_participants": {"SELECT", "INSERT"}, "entity_state": {"SELECT", "INSERT"},
    "entity_corrections": {"SELECT", "INSERT"}, "themes": {"SELECT"},
}
EDITION_TABLES = ("chapters", "ingested_chapters", "chapter_summaries", "entities", "aliases",
                  "edges", "events", "event_participants", "themes", "entity_state",
                  "entity_corrections", "source_objects")


def account_of(dsn: str) -> uuid.UUID:
    with psycopg.connect(dsn) as conn:
        [(account,)] = conn.execute("SELECT account_id FROM notes_account").fetchall()
    return account


def fingerprints(dsn: str, owner, book) -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT file_hash,split_hash FROM books WHERE owner_id=%s AND id=%s",
                            (owner, book)).fetchone()


def editions_of_file(dsn: str, file_hash: str, split_hash: str) -> list[dict]:
    """Every live book of the notes account with these fingerprints, read past row security."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT book.* FROM books AS book JOIN notes_account AS notes"
            " ON notes.account_id=book.owner_id WHERE book.file_hash=%s AND book.split_hash=%s"
            " AND book.deleted_at IS NULL", (file_hash, split_hash)).fetchall()


def door_rows(dsn: str, owner, book) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute("SELECT * FROM story_memory_door(%s)", (book,)).fetchall()


def handed_through(owner, book) -> int:
    """The last chapter the door hands this reader anything of (0 for nothing)."""
    [row] = door_rows(tenant_dsn_value["dsn"], owner, book)
    return max([item["ordinal"] for item in row["completed"]], default=0)


tenant_dsn_value: dict = {}


@pytest.fixture(autouse=True)
def _remember_tenant(tenant_dsn):
    tenant_dsn_value["dsn"] = tenant_dsn
    yield
    tenant_dsn_value.clear()


def claims_of(dsn: str, *owners) -> list:
    """A claimed, started ``story_memory`` job of each owner, in that order."""
    repository = PostgresWorkerRepository(dsn)
    found = {}
    while len(found) < len(owners):
        claim = repository.claim_next(worker_id=f"claimer-{len(found)}",
                                      lease_for=timedelta(seconds=60),
                                      kinds=frozenset({"story_memory"}))
        assert claim is not None and repository.start(claim)
        found[claim.owner_id] = claim
    return [found[owner] for owner in owners]


# ---- 1: exactly one build of a chapter across all readers ---------------------------------------

def test_two_readers_finishing_one_chapter_at_once_make_one_build_one_spend_and_one_chapter(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0044 section 3, "exactly one build". Two readers of one file finish chapter 1 and their
    runs, on two workers, reach the chapter at the same moment (held at a barrier just before
    they name it). The break it catches: both runs extracting and paying for the chapter, two
    copies of it published (or one refused only after both paid), two editions of the file, a
    run that found the chapter taken spending anything or never coming back, or either reader
    not handed the chapter afterwards."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    a.mark(book_a, [1])
    b.mark(book_b, [1])
    barrier = threading.Barrier(2, timeout=60)
    real_begin = PostgresWorkerRepository.begin_story_memory_chapter

    def together(self, *args, **kwargs):
        barrier.wait()
        return real_begin(self, *args, **kwargs)

    monkeypatch.setattr(PostgresWorkerRepository, "begin_story_memory_chapter", together)
    works = [worker(app, extraction, worker_dsn, f"worker-{n}") for n in (1, 2)]
    ran, failed = [None, None], []

    def run(index):
        try:
            ran[index] = works[index].run_once()
        except BaseException as exc:          # pragma: no cover - reported below
            failed.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert failed == [] and ran == [True, True]

    # One build, one spend, one chapter, one edition.
    assert models.parts() == [1] and models.parts("ExtractionVerdict") == [1]
    edition = edition_of(database, a.owner, book_a)
    assert edition is not None and edition == edition_of(database, b.owner, book_b)
    assert len(editions_of_file(database, *fingerprints(database, a.owner, book_a))) == 1
    assert built_in(database, edition) == [(1, "body")]
    assert no_memory_of_their_own(database, a.owner) and no_memory_of_their_own(database, b.owner)
    paid = [someone for someone in (a, b) if spend_of(database, someone.owner) != ([], [])]
    assert len(paid) == 1
    settled_once(database, paid[0].owner)
    [waited] = [someone for someone in (a, b) if someone is not paid[0]]
    built_job = [row for row in jobs_of(database, paid[0].owner)]
    assert [(row["state"], row["payload_metadata"]) for row in built_job] == [
        ("succeeded", slot(1, edition))]
    # The run that found the chapter taken spent nothing, held no slot and asked for its
    # reader's next run a couple of minutes later.
    waiting = jobs_of(database, waited.owner)
    assert [(row["state"], row["payload_metadata"]) for row in waiting] == [
        ("succeeded", {}), ("pending", {})]
    ahead = rows(database, "SELECT run_after-now() AS ahead FROM jobs WHERE id=%s",
                 waiting[1]["id"])[0]["ahead"]
    assert timedelta(minutes=1) < ahead <= timedelta(minutes=2)

    # Its next run: the chapter is there; nothing is extracted or paid; both readers have it.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (waiting[1]["id"],))
    assert drain(works[0]) == 1
    assert models.parts() == [1] and spend_of(database, waited.owner) == ([], [])
    assert built_in(database, edition) == [(1, "body")]
    for someone, book in ((a, book_a), (b, book_b)):
        with HostedBookStore(tenant_dsn, OwnerId(someone.owner)).book(book) as mem:
            assert [row["canonical_name"] for row in mem.view(1).characters()] == ["Mara"]


def test_one_live_edition_per_file_is_the_databases_rule_and_the_door_finds_one(
        memory_app, readers, database, tenant_dsn, worker_dsn):
    """The guarantee step 2 could not build (ADR 0044 step 2 "Built on" (1)), which this step
    needs because editions are now made by concurrent runs. The break it catches: two first
    chapters started at once making two editions of one file (the door joins the edition by
    the fingerprints and would then have two to choose from), a second live edition written
    around the code, a reader's own book marked as an edition, an edition without its
    fingerprints, or the door serving anything but the one marked edition."""
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    file_hash, split_hash = fingerprints(database, a.owner, book_a)
    assert fingerprints(database, b.owner, book_b) == (file_hash, split_hash)
    account = account_of(database)
    for someone, book in ((a, book_a), (b, book_b)):
        assert StoryMemoryQueue(tenant_dsn, OwnerId(someone.owner)).ensure(book)
    claim_a, claim_b = claims_of(worker_dsn, a.owner, b.owner)

    # The code, under real concurrency: two runs making the first edition of a file at once, ten
    # times over (each time the edition is taken away again, as erasing it will).
    for round_ in range(10):
        barrier = threading.Barrier(2, timeout=60)
        made, failed = {}, []

        def make(label, claim):
            try:
                repository = PostgresWorkerRepository(worker_dsn)
                barrier.wait()
                made[label] = repository.edition_of(claim, create=True)
            except BaseException as exc:      # pragma: no cover - reported below
                failed.append(exc)

        threads = [threading.Thread(target=make, args=pair)
                   for pair in (("a", claim_a), ("b", claim_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert failed == [], round_
        [live] = editions_of_file(database, file_hash, split_hash)
        assert made["a"].book_id == made["b"].book_id == live["id"], round_
        assert live["edition_of"] == account == live["owner_id"]
        with psycopg.connect(database) as conn:
            conn.execute("UPDATE books SET deleted_at=now() WHERE id=%s", (live["id"],))

    # The database alone: a second live marked row of one file is refused, also while the first
    # is not yet committed (the second waits for it, then is refused).
    with psycopg.connect(database) as first:
        first.execute("INSERT INTO books (owner_id,title,schema_version,file_hash,split_hash,"
                      "edition_of) VALUES (%s,'edition one',1,%s,%s,%s)",
                      (account, file_hash, split_hash, account))
        outcome = []

        def second():
            try:
                with psycopg.connect(database) as conn:
                    conn.execute("INSERT INTO books (owner_id,title,schema_version,file_hash,"
                                 "split_hash,edition_of) VALUES (%s,'edition two',1,%s,%s,%s)",
                                 (account, file_hash, split_hash, account))
                outcome.append("inserted")
            except psycopg.errors.UniqueViolation:
                outcome.append("refused")

        waiting = threading.Thread(target=second)
        waiting.start()
        waiting.join(2)
        assert waiting.is_alive() and outcome == []      # held by the first, not yet decided
        first.commit()
    waiting.join(30)
    assert outcome == ["refused"]
    [edition] = editions_of_file(database, file_hash, split_hash)

    # Only the notes account's rows may carry the mark, and an edition has both fingerprints.
    for statement, params, error in (
            ("UPDATE books SET edition_of=%s WHERE owner_id=%s AND id=%s",
             (account, a.owner, book_a), psycopg.errors.CheckViolation),
            ("UPDATE books SET edition_of=owner_id WHERE owner_id=%s AND id=%s",
             (a.owner, book_a), psycopg.errors.ForeignKeyViolation),
            ("INSERT INTO books (owner_id,title,schema_version,file_hash,edition_of)"
             " VALUES (%s,'no split',1,%s,%s)", (account, "c" * 64, account),
             psycopg.errors.CheckViolation)):
        with pytest.raises(error):
            with psycopg.connect(database) as conn:
                conn.execute(statement, params)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(tenant_dsn) as conn:
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(a.owner),))
            conn.execute("UPDATE books SET edition_of=owner_id WHERE owner_id=%s AND id=%s",
                         (a.owner, book_a))

    # The door: one row, from the one marked edition. An unmarked copy of the file under the
    # notes account (the operator's command keeps one until step 6) is never an edition.
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO books (owner_id,title,schema_version,file_hash,split_hash,"
                     "created_at) VALUES (%s,'a copy',1,%s,%s,now()-interval '1 day')",
                     (account, file_hash, split_hash))
        conn.execute("INSERT INTO chapters (owner_id,book_id,book_incarnation,chapter_key,"
                     "revealed_at,content_hash,extractor_version)"
                     " SELECT owner_id,id,incarnation,id::text || ':c1.xhtml',1,repeat('d',64),"
                     "'v' FROM books WHERE title='a copy'")
    a.mark(book_a, [1])
    for dsn in (tenant_dsn, database):
        [row] = door_rows(dsn, a.owner, book_a)
        assert row["chapters"] == [] and row["completed"] == []
    assert edition["edition_of"] == account


# ---- 2: a run never holds or publishes anything past its reader's place --------------------------

def test_a_run_for_a_reader_at_chapter_3_never_reads_a_row_of_the_chapters_others_built_past_it(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The first spoiler property of this step. Reader B has built the edition of the long lamp
    book to chapter 9; reader A has finished chapters 1 and 2 and is reading chapter 3. The break
    it catches: A's run reading any row of chapters 3 to 9 (a person, another name, a state, the
    end a later chapter puts on an earlier state, a chapter or its receipt), building or paying
    for anything, or the worker's statement for the notes before a chapter holding anything of
    that chapter or a later one. The statement is checked on its own: for chapter 3 it reads
    exactly what it read when the edition stopped at chapter 2, with Mara's first state still
    open (chapter 6 ends it)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Writer(extraction, monkeypatch)
    client, tokens, _app = memory_app
    a = Reader(client, tokens, "a", readers["a"])
    b = Reader(client, tokens, "b", readers["b"])
    book_a = read_map_gate.upload(client, tokens, "a", LONG)
    book_b = read_map_gate.upload(client, tokens, "b", LONG)
    work = worker(app, extraction, worker_dsn)
    repository = PostgresWorkerRepository(worker_dsn)

    b.mark(book_b, [1, 2])
    assert drain(work) == 2
    [claim] = claims_of_book(worker_dsn, tenant_dsn, b.owner, book_b)
    edition = repository.edition_of(claim)
    repository.succeed(claim)
    at_two = repository.read_edition(edition, before=3)
    b.mark(book_b, list(range(3, 10)))
    assert drain(work, 20) == 7
    assert built_in(database, edition_of(database, b.owner, book_b)) == [
        (n, "body") for n in range(1, 10)]
    at_nine = repository.read_edition(edition, before=3)
    assert at_nine == at_two
    text = json.dumps(at_nine)
    assert not [name for name in NAMES[2:] if name in text]
    assert {row["ordinal"] for row in at_nine["chapters"]} == {1, 2}
    for table in ("entities", "aliases", "states"):
        assert all(row["revealed_at"] < 3 for row in at_nine[table]), table
    assert all(row["invalid_at"] is None for row in at_nine["states"])
    # The bound is what hides the end: read for chapter 10, Mara's first state ends at 6.
    assert 6 in {row["invalid_at"] for row in repository.read_edition(edition,
                                                                      before=10)["states"]}

    # A's run: nothing past chapter 2 is read, by any read of the edition the run makes.
    a.mark(book_a, [1, 2])
    reading = a.call("PUT", f"/api/books/{book_a}/read-page", tpage(3, 1, 10))
    assert reading.status_code == 200, reading.text
    calls, spent, before = list(models.calls), spend_of(database, a.owner), book_memory(
        database, edition_of(database, a.owner, book_a))
    reads = []

    def bounded(name, reach):
        real = getattr(PostgresWorkerRepository, name)

        def recorded(self, *args, **kwargs):
            reads.append((name, reach(*args, **kwargs)))
            return real(self, *args, **kwargs)
        monkeypatch.setattr(PostgresWorkerRepository, name, recorded)

    # The furthest chapter each read can reach.
    bounded("read_edition", lambda edition, *, before: before - 1)
    bounded("next_chapter", lambda edition, *, through: through)
    bounded("chapter_published", lambda edition, ordinal: ordinal)
    bounded("unremembered_before", lambda edition, ordinal: ordinal - 1)
    StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book_a)
    assert drain(work) >= 1
    assert reads and max(reach for _name, reach in reads) <= 2, reads
    assert models.calls == calls and spend_of(database, a.owner) == spent
    assert book_memory(database, edition_of(database, a.owner, book_a)) == before
    assert handed_through(a.owner, book_a) == 2


def claims_of_book(worker_dsn: str, tenant_dsn: str, owner, book) -> list:
    """A claimed, started ``story_memory`` job of one reader's book."""
    assert StoryMemoryQueue(tenant_dsn, OwnerId(owner)).ensure(book)
    return claims_of(worker_dsn, owner)


# ---- 3: only the reader's own copy, and fail closed --------------------------------------------

def test_a_readers_run_reads_only_their_own_copy_and_the_edition_never_has_a_file(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0044 section 2, "Mothli keeps no copy of the file". The break it catches: a run that
    reads another reader's copy (or one Mothli kept) for its chapter's text, so it still builds
    when its own reader's copy is gone and stops when another reader's is; or an edition with a
    file of its own."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    b.mark(book_b, [1])
    assert drain(worker(app, extraction, worker_dsn)) == 1
    edition = edition_of(database, b.owner, book_b)
    assert built_in(database, edition) == [(1, "body")]

    # A's own copy gone (B's is there): A's run builds nothing, extracts nothing. (A worker
    # that has not read A's copy before, so no parse of it is held in its memory.)
    a.mark(book_a, [1, 2])
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    app.state.object_storage.delete(catalog.source_record(book_a)["ref"])
    fresh = CompanionJobs(tenant_dsn=app.state.companion_dsn, settings=app.state.settings,
                          client=app.state.platform_client, storage=app.state.object_storage,
                          sources=SourceParseCache(4), kinds=frozenset({"story_memory"}),
                          extraction_client=extraction, lease_for=timedelta(seconds=60))
    cold = DurableWorker(PostgresWorkerRepository(worker_dsn), fresh, "cold-worker",
                         lease_for=timedelta(seconds=60), retry_after=timedelta(0))
    drain(cold)
    assert built_in(database, edition) == [(1, "body")] and models.parts() == [1]
    assert spend_of(database, a.owner) == ([], [])

    # B's copy gone and A's present: A's next copy of the file builds chapter 2 from it alone.
    assert b.call("DELETE", f"/api/books/{book_b}").status_code == 204
    assert a.call("DELETE", f"/api/books/{book_a}").status_code == 204
    again = read_map_gate.upload(a.client, a.tokens, "a", lamps())
    a.mark(again, [1, 2])
    drain(worker(app, extraction, worker_dsn))
    assert built_in(database, edition) == [(1, "body"), (2, "body")]
    assert models.parts() == [1, 2]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM source_objects WHERE owner_id=%s",
                            (edition[0],)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM source_objects WHERE owner_id=%s"
                            " AND deleted_at IS NULL", (a.owner,)).fetchone()[0] == 1


def test_a_build_fails_closed_on_a_copy_split_another_way_or_an_earlier_chapter_that_differs(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 section 3, "a collision or a mixed split fails closed". The break it catches: a
    run whose reader's copy, split as the code splits it now, is not the edition's split (a
    change to the splitter between import and build would number chapters differently: a
    spoiler), or whose edition holds an earlier chapter whose text is not the copy's, building
    anyway; or such a run spending anything."""
    import app.ingest.extraction.chapter_text as chapter_text

    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, "a")
    a.mark(book, [1, 2])
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 2
    edition = edition_of(database, a.owner, book)
    calls, spent = list(models.calls), spend_of(database, a.owner)

    def last_job():
        return jobs_of(database, a.owner)[-1]

    # The copy split another way: the splitter numbers every chapter one on.
    real = chapter_text.split_hash_of
    monkeypatch.setattr(chapter_text, "split_hash_of", lambda chapters, book_id: real(
        [{**chapter, "ordinal": chapter["ordinal"] + 1} for chapter in chapters], book_id))
    a.mark(book, [3])
    drain(work)
    assert (last_job()["state"], last_job()["sanitized_error"]) == (
        "failed", {"code": "source_integrity", "retryable": False})
    assert built_in(database, edition) == [(1, "body"), (2, "body")]
    assert models.calls == calls and spend_of(database, a.owner) == spent
    monkeypatch.setattr(chapter_text, "split_hash_of", real)

    # An earlier chapter of the edition whose text hash is not the copy's chapter's.
    with psycopg.connect(database) as conn:
        [(kept,)] = conn.execute("SELECT content_hash FROM chapters WHERE owner_id=%s"
                                 " AND book_id=%s AND revealed_at=1", edition).fetchall()
        other = hashlib.sha256(b"another chapter one").hexdigest()
        conn.execute("UPDATE chapters SET content_hash=%s WHERE owner_id=%s AND book_id=%s"
                     " AND revealed_at=1", (other, *edition))
        conn.execute("UPDATE ingested_chapters SET content_hash=%s WHERE owner_id=%s"
                     " AND book_id=%s AND chapter_id=(SELECT id FROM chapters WHERE owner_id=%s"
                     " AND book_id=%s AND revealed_at=1)", (other, *edition, *edition))
    assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book)
    drain(work)
    assert (last_job()["state"], last_job()["sanitized_error"]) == (
        "failed", {"code": "source_integrity", "retryable": False})
    assert built_in(database, edition) == [(1, "body"), (2, "body")]
    assert models.calls == calls and spend_of(database, a.owner) == spent

    # Put right, the same run builds chapter 3.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE chapters SET content_hash=%s WHERE owner_id=%s AND book_id=%s"
                     " AND revealed_at=1", (kept, *edition))
        conn.execute("UPDATE ingested_chapters SET content_hash=%s WHERE owner_id=%s"
                     " AND book_id=%s AND chapter_id=(SELECT id FROM chapters WHERE owner_id=%s"
                     " AND book_id=%s AND revealed_at=1)", (kept, *edition, *edition))
    assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book)
    drain(work)
    assert built_in(database, edition) == [(1, "body"), (2, "body"), (3, "body")]


# ---- 4: the door is the only way in ----------------------------------------------------------------

def test_a_readers_session_never_selects_an_edition_row_before_or_after_their_own_job_built_some(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The second spoiler property. The break it catches: a reader's tenant session reading an
    edition's book row or any of its notes directly (around the door, which bounds them by the
    reader's place), before their own job has built any of it or after it has; row-level
    security not forced on a memory table; or the tenant role gaining a privilege."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    b.mark(book_b, [1])
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 1
    edition = edition_of(database, b.owner, book_b)

    def seen_by_a() -> dict:
        found = {}
        with psycopg.connect(tenant_dsn) as conn, conn.transaction():
            conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(a.owner),))
            found["books"] = conn.execute(
                "SELECT count(*) FROM books WHERE id=%s OR owner_id=%s", (edition[1], edition[0])
            ).fetchone()[0]
            for table in EDITION_TABLES:
                found[table] = conn.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE book_id=%s OR owner_id=%s").format(
                        sql.Identifier(table)), (edition[1], edition[0])).fetchone()[0]
        return found

    nothing = {"books": 0, **{table: 0 for table in EDITION_TABLES}}
    assert book_memory(database, edition)["entities"]
    assert seen_by_a() == nothing                          # before A's job built any
    a.mark(book_a, [1, 2])
    assert drain(work) >= 1
    assert built_in(database, edition) == [(1, "body"), (2, "body")]
    assert spend_of(database, a.owner) != ([], [])         # A's own run built chapter 2
    assert seen_by_a() == nothing                          # and after
    assert handed_through(a.owner, book_a) == 2            # only the door hands it

    with psycopg.connect(database) as conn:
        for table in ("books", *MEMORY_TABLES):
            assert conn.execute("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class"
                                " WHERE oid=%s::regclass", (f"public.{table}",)
                                ).fetchone()[0], table
    for table, privileges in TENANT_BEFORE.items():
        assert TENANT_TABLE_PRIVILEGES.get(table, frozenset()) == privileges, table


# ---- 5: no edition row names a reader --------------------------------------------------------------

def test_no_edition_row_names_a_reader_their_book_their_title_or_their_file(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The third spoiler property (ADR 0044 section 2: a neutral title, no author, no source).
    The break it catches: an edition, or any row under it, carrying the reader's account, book
    id or incarnation, their title, author or file name; a chapter key published as the
    reader's (``<their book>:<part>``) rather than the edition's, or the door no longer turning
    it back into theirs; or a publish that names the reader anywhere let through."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, "a")
    a.mark(book, [1, 2, 3])
    assert drain(worker(app, extraction, worker_dsn)) == 3
    edition = edition_of(database, a.owner, book)
    assert built_in(database, edition) == [(1, "body"), (2, "body"), (3, "body")]
    account = account_of(database)
    file_hash, _split = fingerprints(database, a.owner, book)
    with psycopg.connect(database, row_factory=dict_row) as conn:
        row = conn.execute("SELECT * FROM books WHERE owner_id=%s AND id=%s", edition).fetchone()
        mine = conn.execute("SELECT incarnation,title,author FROM books WHERE id=%s",
                            (book,)).fetchone()
        with psycopg.connect(database) as plain:
            tables = owner_tables(plain)
        everything = {table: [found["t"] for found in conn.execute(
            sql.SQL("SELECT t::text AS t FROM {} AS t WHERE owner_id=%s").format(
                sql.Identifier(table)), (account,)).fetchall()]
            for table in tables if table != "audit_events"}
        keys = [found["chapter_key"] for found in conn.execute(
            "SELECT chapter_key FROM chapters WHERE owner_id=%s AND book_id=%s",
            edition).fetchall()]
    assert (row["title"], row["author"], row["source_kind"], row["source_id"],
            row["edition_of"]) == (f"edition {file_hash[:12]}", None, None, None, account)
    assert (mine["title"], mine["author"]) == ("The Lamp Book", "Ada Writer")
    words = [str(a.owner), book, str(mine["incarnation"]), "Reader A", "The Lamp Book",
             "Ada Writer", "lamp.epub"]
    text = json.dumps(everything)
    assert not [word for word in words if word in text]
    assert sorted(keys) == [f"{edition[1]}:c{n}.xhtml" for n in (1, 2, 3)]
    [door] = door_rows(tenant_dsn, a.owner, book)
    assert sorted(item["key"] for item in door["chapters"]) == [
        f"{book}:c{n}.xhtml" for n in (1, 2, 3)]
    with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
        from tests.parity.test_story_memory_parity import hosted_atoms
        assert mem.completion_frontier(hosted_atoms(app, a.owner, book)) == 3

    # A publish that names the reader anywhere is refused whole, before anything is written.
    repository, claim = claimed(app, worker_dsn, a.owner, book)
    target = repository.edition_of(claim)
    before = book_memory(database, edition)
    nothing = {name: [] for name in ("summaries", "entities", "aliases", "states",
                                     "closed_states", "edges", "events", "participants",
                                     "themes")}
    summary = {"kind": "chapter", "summary": f"Read by {book}.", "revealed_at": 4,
               "extractor_version": "v"}
    for chapter, rows_ in (
            ({"ordinal": 4, "key": f"{book}:c4.xhtml", "title": f"Part of {a.owner}"}, nothing),
            ({"ordinal": 4, "key": f"{book}:c4.xhtml"}, {**nothing, "summaries": [summary]})):
        with pytest.raises(JobIdempotencyError):
            repository.commit_chapter_memory(claim, edition=target, copied="",
                                             chapter=chapter, receipt={}, rows=rows_)
    assert book_memory(database, edition) == before


# ---- 6: failures counted per edition ----------------------------------------------------------------

def test_two_readers_failed_runs_of_one_chapter_count_once_for_the_edition(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 section 3, failures per edition: "one bad chapter is paid for twice per file, not
    twice per reader". Two readers of one file, the review rejecting chapter 2 every time. The
    break it catches: each reader's runs counted alone (four failed runs, the chapter paid four
    times), or the chapter kept unremembered after fewer than two failed runs in all."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, reject={2})
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    a.mark(book_a, [1, 2])
    b.mark(book_b, [1, 2])
    drain(worker(app, extraction, worker_dsn), 20)
    edition = edition_of(database, a.owner, book_a)
    assert built_in(database, edition) == [(1, "body"), (2, "unremembered")]
    every = jobs_of(database, a.owner) + jobs_of(database, b.owner)
    failed = [row for row in every if row["state"] == "failed"]
    assert [(row["sanitized_error"], row["payload_metadata"]) for row in failed] == [
        ({"code": "grounding_rejected", "retryable": False}, slot(2, edition))] * 2
    # Two runs of chapter 2 in all (its extraction and one repair each), chapter 1 once.
    assert Counter(models.parts()) == {1: 1, 2: 4}


def test_three_unremembered_in_a_row_stop_the_edition_for_every_reader(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 section 3: "A stopped edition stays stopped for every reader". Two readers of the
    long lamp book, the review rejecting chapters 2, 3 and 4. The break it catches: the stop
    counted per reader (a second reader paying for chapters 2 to 4 again, or carrying the
    edition on past the stop), or a chapter paid for more than twice in all."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    writer = Writer(extraction, monkeypatch, reject={2, 3, 4})
    client, tokens, _app = memory_app
    a = Reader(client, tokens, "a", readers["a"])
    b = Reader(client, tokens, "b", readers["b"])
    book_a = read_map_gate.upload(client, tokens, "a", LONG)
    book_b = read_map_gate.upload(client, tokens, "b", LONG)
    a.mark(book_a, list(range(1, 9)))
    b.mark(book_b, list(range(1, 9)))
    work = worker(app, extraction, worker_dsn)
    drain(work, 60)
    edition = edition_of(database, a.owner, book_a)
    assert built_in(database, edition) == [(1, "body"), *[(n, "unremembered") for n in (2, 3, 4)]]
    assert Counter(writer.parts()) == {1: 1, 2: 4, 3: 4, 4: 4}
    every = jobs_of(database, a.owner) + jobs_of(database, b.owner)
    assert Counter(row["payload_metadata"]["chapter"] for row in every
                   if row["state"] == "failed"
                   and row["sanitized_error"]["code"] == "grounding_rejected") == {2: 2, 3: 2,
                                                                                   4: 2}
    assert "too_many_unremembered" in {row["sanitized_error"]["code"] for row in every
                                       if row["state"] == "failed"}
    calls = list(writer.calls)
    for someone, book in ((a, book_a), (b, book_b)):
        someone.mark(book, [9])
        StoryMemoryQueue(app.state.companion_dsn, OwnerId(someone.owner)).ensure(book)
    drain(work, 60)
    assert writer.calls == calls and len(built_in(database, edition)) == 4


# ---- 7: when the build slot frees after a lease expires -----------------------------------------

def test_an_expired_lease_frees_the_build_slot_at_the_next_claim_and_not_before(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 "could not verify" 1. A's run holds chapter 1's slot and dies mid-chapter; B's run
    meanwhile finds the chapter taken. The break it catches: a slot freed by the lease's expiry
    alone while no claim has run (it is not: the job stays ``running`` until the claim recovers
    it), a slot never freed (B waiting for good), the recovered job keeping its slot into its
    next attempt, or the chapter built twice."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    a.mark(book_a, [1])
    first = worker(app, extraction, worker_dsn, "first")
    second = worker(app, extraction, worker_dsn, "second")
    seen = {}

    def b_meanwhile_then_a_dies(schema, part):
        if not seen:
            b.mark(book_b, [1])
            seen["b ran"] = second.run_once()
            raise KeyboardInterrupt("A's worker died mid-chapter")

    models = Lamps(extraction, monkeypatch, hook=b_meanwhile_then_a_dies)
    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    edition = edition_of(database, a.owner, book_a)

    def holders() -> list:
        return [(row["owner_id"], row["state"]) for row in rows(
            database, "SELECT owner_id,state FROM jobs WHERE kind='story_memory'"
                      " AND state IN ('leased','running') AND payload_metadata->>'edition'=%s"
                      " AND payload_metadata->>'chapter'='1'", str(edition[1]))]

    assert seen == {"b ran": True}
    assert holders() == [(a.owner, "running")]
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, b.owner)] == [
        ("succeeded", {}), ("pending", {})]
    expire_lease(database, a.owner)
    assert holders() == [(a.owner, "running")]             # expired, and still held
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE owner_id=%s AND state='pending'",
                     (b.owner,))
    # Any claim, by any worker for any kind, recovers the expired lease first.
    assert PostgresWorkerRepository(worker_dsn).claim_next(
        worker_id="notes-only", lease_for=timedelta(seconds=60),
        kinds=frozenset({"chapter_notes"})) is None
    assert holders() == []
    [a_job] = jobs_of(database, a.owner)
    assert (a_job["state"], a_job["payload_metadata"]) == ("pending", slot(1, edition))

    models.hook = None
    assert drain(second) == 2                               # B's run builds it; A's finds it
    assert built_in(database, edition) == [(1, "body")]
    assert models.parts() == [1, 1]                         # A's dead attempt, and B's
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, a.owner)] == [
        ("succeeded", {"chapter": 1})]                     # the slot was not carried over
    settled_once(database, a.owner)                         # A's dead calls settled once
    settled_once(database, b.owner)


# ---- 8: a reader's own rows from before ------------------------------------------------------------

def test_a_readers_own_rows_from_before_are_left_and_the_edition_is_built_beside_them(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """What this step does with a reader's own story memory rows built while their own story
    memory was on (only in tests: nothing is deployed). The break it catches: a run that reads
    them (they are not the edition's and were built by another path), writes beside them, or
    deletes them; or the reader handed them rather than the edition once the door serves the
    book (the door comes first, ADR 0044 section 4)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, "a")
    # The reader's own chapter 1, as the per-reader path published it: a person nobody's
    # extraction makes.
    with psycopg.connect(database) as conn:
        [(incarnation,)] = conn.execute("SELECT incarnation FROM books WHERE id=%s",
                                        (book,)).fetchall()
        chapter = uuid.uuid4()
        catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
        [first, *_rest] = HostedBookSource(app.state.object_storage, catalog,
                                           app.state.companion_sources).verified_source(
            book, catalog.get_book(book))[1]
        conn.execute("INSERT INTO chapters (owner_id,id,book_id,book_incarnation,chapter_key,"
                     "revealed_at,content_hash,extractor_version) VALUES (%s,%s,%s,%s,%s,1,%s,'v')",
                     (a.owner, chapter, book, incarnation, first["key"],
                      hashlib.sha256(first["text"].encode()).hexdigest()))
        conn.execute("INSERT INTO entities (owner_id,book_id,book_incarnation,source_chapter_id,"
                     "canonical_name,entity_type,revealed_at,extractor_version)"
                     " VALUES (%s,%s,%s,%s,'Oldname','character',1,'v')",
                     (a.owner, book, incarnation, chapter))
        conn.execute("INSERT INTO ingested_chapters (owner_id,book_id,book_incarnation,"
                     "chapter_id,content_hash,completed_at) SELECT owner_id,book_id,"
                     "book_incarnation,id,content_hash,now() FROM chapters WHERE id=%s",
                     (chapter,))
    own = memory_of(database, a.owner)
    assert own["entities"]
    a.mark(book, [1, 2])
    drain(worker(app, extraction, worker_dsn))
    assert {row["state"] for row in jobs_of(database, a.owner)} == {"succeeded"}
    assert models.parts() == [1, 2]                         # built for the edition, from 1
    assert memory_of(database, a.owner) == own              # left exactly as they were
    edition = edition_of(database, a.owner, book)
    assert built_in(database, edition) == [(1, "body"), (2, "body")]
    assert "Oldname" not in json.dumps(book_memory(database, edition))
    with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
        assert mem.shared
        assert sorted(row["canonical_name"] for row in mem._audit_all("entities")) == [
            "Corvus", "Mara"]
