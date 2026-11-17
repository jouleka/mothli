"""One chapter of story memory, extracted on a working copy and published to Postgres (ADR 0042
part 2), against real PostgreSQL.

The parity of what is published with local mode is in ``tests/parity/test_story_memory_parity.py``.
Here: a chapter is never published twice and never in part, and nothing of it is seen before its
receipt; a lost or cancelled lease publishes nothing; a reader's correction during extraction makes
the publish refuse and the chapter be prepared again against the corrected roster; a crash before
the chapter's calls are settled is settled once by the next run; no chapter text, passage or
vector reaches Postgres; two readers with the same book file never touch each other's rows, on
the row-security role and through a superuser connection; removing the book and erasing the
account leave nothing; the worker role holds exactly its grants; and the chapter is paid within
the reader's allowance, admitted whole. Stand-in models only: nothing is called and nothing paid.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import conninfo, sql
from psycopg.rows import dict_row

import app.hosted.companion.story_memory as story_memory_module
import app.hosted.jobs as jobs_module
from app.cost.limits import CostCeilingExceeded
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.store import ChapterCatalog, HostedCompanionCatalog
from app.hosted.companion.story_memory import ChapterOutOfOrder
from app.hosted.jobs import (
    ChapterAlreadyPublished,
    JobIdempotencyError,
    LostLeaseError,
    PostgresWorkerRepository,
    StoryMemoryMoved,
    WorkerConfigurationError,
)
from app.hosted.limits import update_limits
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_account
from app.hosted.tenant.grants import (
    TENANT_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
    purge_grants,
)
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    Reader,
    restore_caps,
)
from tests.parity import test_story_memory_parity as parity
from tests.parity.test_story_memory_parity import (
    CHAPTERS,
    Extractor,
    claimed,
    extraction_client,
    harbour_book,
    hosted_book,
    remember,
)

pytestmark = pytest.mark.postgres

hosted_app = parity.hosted_app
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
worker_dsn = jobs_gate.worker_dsn

MEMORY_TABLES = ("chapters", "ingested_chapters", "chapter_summaries", "entities", "aliases",
                 "edges", "events", "event_participants", "themes", "entity_state",
                 "entity_corrections", "chunks", "chunk_embeddings")
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def memory_of(dsn: str, owner: uuid.UUID) -> dict[str, list[str]]:
    """Every story memory row of one owner, read past row-level security."""
    with psycopg.connect(dsn) as conn:
        return {table: sorted(row[0] for row in conn.execute(
            sql.SQL("SELECT t::text FROM {} AS t WHERE owner_id=%s").format(sql.Identifier(table)),
            (owner,)).fetchall()) for table in MEMORY_TABLES}


def spend_of(dsn: str, owner: uuid.UUID) -> tuple[list[dict], list[dict]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return (conn.execute("SELECT * FROM cost_reservations WHERE owner_id=%s "
                             "ORDER BY created_at,id", (owner,)).fetchall(),
                conn.execute("SELECT * FROM cost_ledger WHERE owner_id=%s "
                             "ORDER BY recorded_at,id", (owner,)).fetchall())


def settled_once(dsn: str, owner: uuid.UUID) -> list[dict]:
    """Every reservation of the owner is settled or released, and each settled one is in the
    ledger exactly once, at its noted size. Returns the ledger."""
    reservations, ledger = spend_of(dsn, owner)
    assert reservations and all(row["state"] in ("settled", "released") for row in reservations)
    keys = [row["idempotency_key"] for row in ledger]
    assert len(keys) == len(set(keys))
    by_key = {row["idempotency_key"]: row for row in ledger}
    for row in reservations:
        if row["state"] == "settled":
            assert by_key[row["idempotency_key"]]["usd"] == row["reserved_usd"]
    assert len(ledger) == sum(row["state"] == "settled" for row in reservations)
    return ledger


def entity_id(dsn: str, owner: uuid.UUID, name: str) -> uuid.UUID:
    with psycopg.connect(dsn) as conn:
        [(found,)] = conn.execute("SELECT id FROM entities WHERE owner_id=%s AND canonical_name=%s",
                                  (owner, name)).fetchall()
    return found


@pytest.fixture()
def harbour(hosted_app, readers, monkeypatch):
    """Reader A's harbour book on the hosted app, with the stand-in extraction client."""
    _client, _tokens, app = hosted_app
    client = extraction_client()
    extractor = Extractor(client, monkeypatch)
    book = hosted_book(hosted_app, "a", harbour_book())
    return app, client, extractor, readers["a"], book


# ---- 2: never twice, never half, nothing seen before the receipt ---------------------------------

def test_a_publish_cut_off_before_it_commits_leaves_nothing_and_is_never_seen_half(
        harbour, worker_dsn, database, tenant_dsn, monkeypatch):
    """The break it catches: rows published in more than one transaction, or the receipt not
    last, so a reply could read a chapter's people without its receipt, or a worker that dies
    mid-publish leaves half a chapter."""
    app, client, extractor, owner, book = harbour
    seen = []
    real_event = jobs_module.record_event

    def dies_in_the_publish(conn, **values):
        if values["action"] == "memory.publish":
            # Every row and the receipt are written in this transaction, not yet committed.
            assert conn.execute("SELECT count(*) FROM entities WHERE owner_id=%s",
                                (owner,)).fetchone()["count"] == 2
            for dsn in (tenant_dsn, database):
                with HostedBookStore(dsn, OwnerId(owner)).book(book) as mem:
                    seen.append((mem._audit_all("entities"), mem.view(9).chapters()))
            raise RuntimeError("the worker died here")
        return real_event(conn, **values)

    monkeypatch.setattr(jobs_module, "record_event", dies_in_the_publish)
    with pytest.raises(RuntimeError, match="died here"):
        remember(app, worker_dsn, owner, book, client, 1)
    assert seen == [([], []), ([], [])]
    assert all(rows == [] for rows in memory_of(database, owner).values())
    reservations, _ledger = spend_of(database, owner)
    assert {row["purpose"] for row in settled_once(database, owner)} >= {
        "extraction-commit-aborted", "extraction-review"}
    assert all(row["state"] != "reserved" for row in reservations)

    monkeypatch.setattr(jobs_module, "record_event", real_event)
    assert remember(app, worker_dsn, owner, book, client, 1) is True
    with HostedBookStore(tenant_dsn, OwnerId(owner)).book(book) as mem:
        assert [row["canonical_name"] for row in mem.view(1).characters()] == ["Mara", "Corvus"]


def test_a_chapter_is_published_once_and_a_second_publish_changes_nothing(
        harbour, worker_dsn, database, monkeypatch):
    """The break it catches: a retried or repeated publish that adds a second copy of a
    chapter's people, aliases or receipt, or pays for the chapter again."""
    app, client, extractor, owner, book = harbour
    payloads = []
    real_commit = PostgresWorkerRepository.commit_chapter_memory

    def recorded(self, claim, **publish):
        payloads.append((claim, publish))
        return real_commit(self, claim, **publish)

    monkeypatch.setattr(PostgresWorkerRepository, "commit_chapter_memory", recorded)
    assert remember(app, worker_dsn, owner, book, client, 1) is True
    before, calls, spend = memory_of(database, owner), list(extractor.calls), spend_of(
        database, owner)

    # The same publish again, as a worker that lost track of its first would send it.
    repository, claim = claimed(app, worker_dsn, owner, book)
    with pytest.raises(ChapterAlreadyPublished):
        repository.commit_chapter_memory(claim, **payloads[0][1])
    repository.succeed(claim)
    # And the chapter run again: nothing to extract, nothing to pay, nothing written.
    assert remember(app, worker_dsn, owner, book, client, 1) is False
    assert memory_of(database, owner) == before
    assert extractor.calls == calls
    assert spend_of(database, owner) == spend
    assert len(before["ingested_chapters"]) == 1 and len(before["entities"]) == 2

    # Out of order: chapter 3 before chapter 2 is refused before anything is spent.
    with pytest.raises(ChapterOutOfOrder):
        remember(app, worker_dsn, owner, book, client, 3)
    assert extractor.calls == calls and spend_of(database, owner) == spend


# ---- 3: a lost lease publishes nothing ------------------------------------------------------------

def expire_lease(dsn: str, owner: uuid.UUID) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE job_attempts SET leased_at=now()-interval '2 seconds',"
                     "lease_expires_at=now()-interval '1 second' "
                     "WHERE owner_id=%s AND finished_at IS NULL", (owner,))


def test_a_lease_another_worker_took_spends_and_publishes_nothing(harbour, worker_dsn, database):
    """The break it catches: a run that goes on after its lease went to another worker, paying
    for the chapter again or publishing beside the new owner of the job."""
    app, client, extractor, owner, book = harbour
    repository, first = claimed(app, worker_dsn, owner, book, worker="first")
    expire_lease(database, owner)
    second = repository.claim_next(worker_id="second", lease_for=timedelta(seconds=60),
                                   kinds=frozenset({"chapter_notes"}))
    assert second is not None and second.job_id == first.job_id
    with pytest.raises(LostLeaseError):
        remember(app, worker_dsn, owner, book, client, 1, claim=(repository, first))
    assert extractor.calls == []
    assert spend_of(database, owner) == ([], [])
    assert all(rows == [] for rows in memory_of(database, owner).values())


@pytest.mark.parametrize("how", ["expired", "cancelled"])
def test_a_lease_lost_during_the_chapter_is_refused_at_the_publish(harbour, worker_dsn, database,
                                                                  monkeypatch, how):
    """The break it catches: a publish that trusts the lease checks made before the model calls,
    so a run whose lease expired, or whose job was cancelled (its book removed), while the model
    was answering still publishes. The lease check before each call is turned off here, so only
    the publish's own fence stands."""
    app, client, extractor, owner, book = harbour

    def lose(marker):
        if how == "expired":
            expire_lease(database, owner)
        else:
            with psycopg.connect(database) as conn:
                conn.execute("UPDATE jobs SET cancellation_requested_at=now() WHERE owner_id=%s",
                             (owner,))

    extractor.hook = lose
    with pytest.raises(LostLeaseError):
        remember(app, worker_dsn, owner, book, client, 1, hold=lambda: None)
    assert extractor.calls.count("Extraction") == 1
    assert all(rows == [] for rows in memory_of(database, owner).values())
    assert {row["purpose"] for row in settled_once(database, owner)} >= {
        "extraction-commit-aborted"}


# ---- 4: a correction during extraction --------------------------------------------------------

def test_a_correction_during_extraction_is_respected_and_the_chapter_prepared_again(
        hosted_app, harbour, worker_dsn, database, tenant_dsn, monkeypatch):
    """The break it catches: a chapter extracted against the roster from before a reader's
    correction published as it was prepared, tying its events and her new state to the identity
    the reader just retired (invisible from the correction on), so the chapter's facts about her
    are lost and the correction undone. Local mode prepares again on ``StalePreparedRoster``;
    hosted mode must too, and without paying for the extraction again."""
    app, client, extractor, owner, book = harbour
    http, tokens, _app = hosted_app
    a = Reader(http, tokens, "a", owner)
    for ordinal in (1, 2, 3):
        assert remember(app, worker_dsn, owner, book, client, ordinal) is True
    a.mark(book, [1, 2, 3])
    mara = entity_id(database, owner, "Mara")
    corrected = []

    def correct(marker):
        if marker == "rescued" and not corrected:
            response = a.call("POST", f"/api/books/{book}/memory-corrections",
                              {"source_entity_id": str(mara), "canonical_name": "Marabel",
                               "reason": "her full name", "bookmark": 3})
            assert response.status_code == 200, response.text
            corrected.append(response.json())

    outcomes = []
    real_commit = PostgresWorkerRepository.commit_chapter_memory

    def recorded(self, claim, **publish):
        try:
            result = real_commit(self, claim, **publish)
        except Exception as exc:
            outcomes.append(type(exc))
            raise
        outcomes.append("published")
        return result

    monkeypatch.setattr(PostgresWorkerRepository, "commit_chapter_memory", recorded)
    extractor.hook = correct
    calls = len(extractor.calls)
    assert remember(app, worker_dsn, owner, book, client, 4) is True
    assert corrected and outcomes == [StoryMemoryMoved, "published"]
    assert extractor.calls[calls:].count("Extraction") == 1          # not paid for again

    marabel = entity_id(database, owner, "Marabel")
    with psycopg.connect(database, row_factory=dict_row) as conn:
        at_four = conn.execute(
            "SELECT 'state' AS kind,entity_id FROM entity_state"
            " WHERE owner_id=%(o)s AND revealed_at=4"
            " UNION ALL SELECT 'participant',entity_id FROM event_participants"
            " WHERE owner_id=%(o)s AND revealed_at=4", {"o": owner}).fetchall()
        closed = conn.execute("SELECT entity_id FROM entity_state WHERE owner_id=%s"
                              " AND invalid_at=4", (owner,)).fetchall()
        named = conn.execute("SELECT canonical_name FROM entities WHERE owner_id=%s"
                             " AND revealed_at=4", (owner,)).fetchall()
    assert {("state", marabel), ("participant", marabel)} <= {
        (row["kind"], row["entity_id"]) for row in at_four}
    assert mara not in {row["entity_id"] for row in at_four}
    assert [row["entity_id"] for row in closed] == [marabel]      # the state copied to her
    assert [row["canonical_name"] for row in named] == ["Rose"]   # no second Mara made
    with HostedBookStore(tenant_dsn, OwnerId(owner)).book(book) as mem:
        view = mem.view(4)
        assert [row["canonical_name"] for row in view.characters()] == [
            "Corvus", "Vex", "Marabel", "Rose"]
        assert [event["summary"] for event in view.timeline()][-1] == "The Keeper lit the lantern."


# ---- 5: a crash between the publish and settling the costs -------------------------------------

def test_a_crash_after_the_publish_is_settled_once_by_the_next_run(harbour, worker_dsn, database,
                                                                   monkeypatch):
    """The break it catches: a chapter's deferred extraction calls left reserved for good after
    a crash between the publish and settling them (a reader's allowance held by money spent
    long ago), or settled twice (charged twice) when the chapter is run again."""
    app, client, extractor, owner, book = harbour
    real_finalize = ChapterCatalog.finalize_ingest

    def crashes(self, book_id, n, **kwargs):
        raise KeyboardInterrupt("the worker died after the publish")

    monkeypatch.setattr(ChapterCatalog, "finalize_ingest", crashes)
    with pytest.raises(KeyboardInterrupt):
        remember(app, worker_dsn, owner, book, client, 1)
    reservations, _ledger = spend_of(database, owner)
    left = [row for row in reservations if row["state"] == "reserved"]
    assert [row["purpose"] for row in left] == ["extraction"]
    assert left[0]["actual_usd"] == left[0]["reserved_usd"]           # its noted size
    assert len(memory_of(database, owner)["ingested_chapters"]) == 1   # published

    monkeypatch.setattr(ChapterCatalog, "finalize_ingest", real_finalize)
    calls = len(extractor.calls)
    assert remember(app, worker_dsn, owner, book, client, 2) is True   # the next chapter's run
    ledger = settled_once(database, owner)
    assert [row["purpose"] for row in ledger].count("extraction") == 1
    assert extractor.calls[calls:] == []                               # the map page: no call
    assert remember(app, worker_dsn, owner, book, client, 2) is False  # and again: no change
    assert settled_once(database, owner) == ledger


def test_a_run_that_died_mid_chapter_is_settled_and_the_chapter_done_on_the_next(
        harbour, worker_dsn, database):
    """The break it catches: a chapter refused for good ("outstanding extraction cost
    reservations") after a worker died while its calls were open, or those calls never settled."""
    app, client, extractor, owner, book = harbour

    def dies(marker):
        raise KeyboardInterrupt("the worker died mid-chapter")

    extractor.hook = dies
    with pytest.raises(KeyboardInterrupt):
        remember(app, worker_dsn, owner, book, client, 1)
    extractor.hook = None
    assert any(row["state"] == "reserved" for row in spend_of(database, owner)[0])
    assert remember(app, worker_dsn, owner, book, client, 1) is True
    ledger = settled_once(database, owner)
    assert "extraction-abandoned" in {row["purpose"] for row in ledger}


# ---- 6: nothing that must stay out reaches Postgres --------------------------------------------

def test_no_chapter_text_passage_or_vector_is_published(harbour, worker_dsn, database):
    """The break it catches: the working copy's raw chapter text, its passages or their vectors
    (all of which the shared commit writes locally) copied into Postgres with the memory."""
    app, client, extractor, owner, book = harbour
    with psycopg.connect(database) as conn:
        tables = owner_tables(conn)

    def everything():
        with psycopg.connect(database) as conn:
            return {table: {row[0] for row in conn.execute(
                sql.SQL("SELECT t::text FROM {} AS t WHERE owner_id=%s").format(
                    sql.Identifier(table)), (owner,)).fetchall()} for table in tables}

    before = everything()
    for ordinal in (1, 2, 3, 4):
        assert remember(app, worker_dsn, owner, book, client, ordinal) is True
    after = everything()
    added = [row for table in tables for row in after[table] - before[table]]
    assert any("Corvus" in row for row in added)                      # the memory is there
    for text in CHAPTERS.values():
        sentences = [part.strip() + "." for part in text.split(".") if part.strip()]
        for first, second in zip(sentences, sentences[1:] + sentences[:1]):
            passage = f"{first} {second}"                              # any run of two sentences
            assert not any(passage in row for row in added), passage
    stored = memory_of(database, owner)
    assert stored["chunks"] == [] and stored["chunk_embeddings"] == []
    assert after["book_search_documents"] == before["book_search_documents"]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM entity_corrections WHERE owner_id=%s",
                            (owner,)).fetchone()[0] == 0


# ---- 7: two readers with the same book file -----------------------------------------------------

def test_one_readers_chapter_never_reads_or_writes_anothers_rows(hosted_app, readers, worker_dsn,
                                                                 database, tenant_dsn,
                                                                 monkeypatch):
    """The break it catches: a working copy or a publish that misses an owner predicate, so
    reader A's chapter is extracted against reader B's people (both have the same book file) or
    lands in B's book; checked with row-level security and, through a superuser connection,
    with the code's own predicates alone."""
    _client, _tokens, app = hosted_app
    client = extraction_client()
    Extractor(client, monkeypatch)
    a, b = readers["a"], readers["b"]
    a_book = hosted_book(hosted_app, "a", harbour_book())
    b_book = hosted_book(hosted_app, "b", harbour_book())
    for ordinal in (1, 2, 3):
        assert remember(app, worker_dsn, b, b_book, client, ordinal) is True
    b_rows = memory_of(database, b)

    # Chapter 1 through a superuser connection (the code's own predicates alone), the rest on
    # the tenant role with row-level security.
    assert remember(app, worker_dsn, a, a_book, client, 1, tenant=database) is True
    assert remember(app, worker_dsn, a, a_book, client, 2) is True
    assert remember(app, worker_dsn, a, a_book, client, 3) is True

    assert memory_of(database, b) == b_rows
    with psycopg.connect(database) as conn:
        for table in MEMORY_TABLES:
            mixed = conn.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE (owner_id=%s AND book_id<>%s)"
                        " OR (owner_id=%s AND book_id<>%s)").format(sql.Identifier(table)),
                (a, uuid.UUID(a_book), b, uuid.UUID(b_book))).fetchone()[0]
            assert mixed == 0, table
        # A's chapter 3 merged "the Keeper" into A's own Mara, never B's.
        rows = conn.execute("SELECT alias.owner_id,entity.owner_id FROM aliases AS alias"
                            " JOIN entities AS entity ON entity.id=alias.entity_id"
                            " WHERE alias.surface_form='the Keeper'").fetchall()
    assert sorted(rows) == sorted([(a, a), (b, b)])
    for dsn in (tenant_dsn, database):
        with HostedBookStore(dsn, OwnerId(b)).book(b_book) as mem:
            assert len(mem._audit_all("entities")) == 3
        with pytest.raises(LookupError):
            with HostedBookStore(dsn, OwnerId(b)).book(a_book):
                pass

    # A claim of A's job cannot be turned on B's book, by the run or by the publish.
    repository, claim = claimed(app, worker_dsn, a, a_book)
    from dataclasses import replace
    forged = replace(claim, book_id=uuid.UUID(b_book))
    with pytest.raises(LookupError):
        remember(app, worker_dsn, a, a_book, client, 4, claim=(repository, forged))
    with pytest.raises(JobIdempotencyError):
        repository.commit_chapter_memory(
            claim, book_id=uuid.UUID(b_book), book_incarnation=claim.book_incarnation,
            copied="", chapter={"ordinal": 4, "key": "x"}, receipt={},
            rows={name: [] for name in ("summaries", "entities", "aliases", "states",
                                        "closed_states", "edges", "events", "participants",
                                        "themes")})
    assert memory_of(database, b) == b_rows


# ---- 8: erasure still works ----------------------------------------------------------------------

def test_removing_the_book_and_erasing_the_account_leave_no_memory_row(
        hosted_app, harbour, worker_dsn, database, tenant_dsn, admin_dsn):
    """The break it catches: a published row that does not hang from its chapter's receipt (or
    lands under another chapter or owner), so removing the book or erasing the account leaves
    a reader's story memory behind."""
    app, client, extractor, owner, book = harbour
    for ordinal in (1, 2, 3, 4):
        assert remember(app, worker_dsn, owner, book, client, ordinal) is True
    assert memory_of(database, owner)["aliases"]
    assert asyncio.run(PostgresTenantRepository(tenant_dsn).remove_book(
        OwnerId(owner), uuid.UUID(book)))
    assert all(rows == [] for rows in memory_of(database, owner).values())

    # A second book, built again, and the account erased by the purge under its own role.
    second = hosted_book(hosted_app, "a", harbour_book())
    for ordinal in (1, 2, 3):
        assert remember(app, worker_dsn, owner, second, client, ordinal) is True
    assert memory_of(database, owner)["entities"]
    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
        conn.execute("UPDATE users SET status='deleting',erase_after=now()-interval '1 day'"
                     " WHERE id=%s", (owner,))
    with _role(admin_dsn, database, bypass=True, tables=tables) as purge_dsn:
        assert purge_account(PostgresAccountPurge(purge_dsn), app.state.object_storage, owner)
    assert all(rows == [] for rows in memory_of(database, owner).values())


# ---- 9: the worker's grants ------------------------------------------------------------------------

def held_privileges(dsn: str, role: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        return {privilege for privilege in ALL_PRIVILEGES if conn.execute(
            "SELECT has_table_privilege(%s,%s,%s)", (role, f"public.{table}", privilege)
        ).fetchone()[0]}


def test_the_worker_role_holds_exactly_what_a_publish_needs(worker_dsn, database, tenant_dsn):
    """The break it catches: a worker role that can read or change more of a reader's story
    memory than a publish needs (or less, so the publish fails in production), or a startup
    check that would not notice one privilege more or fewer."""
    role = conninfo.conninfo_to_dict(worker_dsn)["user"]
    expected = {
        "chapters": {"SELECT", "INSERT"}, "ingested_chapters": {"SELECT", "INSERT"},
        "chapter_summaries": {"INSERT"}, "entities": {"SELECT", "INSERT"},
        "aliases": {"SELECT", "INSERT"}, "entity_state": {"SELECT", "INSERT", "UPDATE"},
        "edges": {"INSERT"}, "events": {"INSERT"}, "event_participants": {"INSERT"},
        "themes": {"INSERT"}, "entity_corrections": set(), "chunks": set(),
        "chunk_embeddings": set(),
    }
    for table, privileges in expected.items():
        assert WORKER_TABLE_PRIVILEGES.get(table, frozenset()) == privileges, table
        assert held_privileges(database, role, table) == privileges, table
    # The tenant role gains nothing: it already reads memory and writes corrections.
    assert TENANT_TABLE_PRIVILEGES["edges"] == {"SELECT", "INSERT"}
    assert TENANT_TABLE_PRIVILEGES["themes"] == {"SELECT"}
    tenant = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    assert held_privileges(database, tenant, "events") == {"SELECT"}
    check = PostgresWorkerRepository(worker_dsn)
    check.check_runtime_role()
    for grant, undo in (
        ("GRANT DELETE ON entities TO {}", "REVOKE DELETE ON entities FROM {}"),
        ("GRANT UPDATE ON aliases TO {}", "REVOKE UPDATE ON aliases FROM {}"),
        ("GRANT SELECT ON themes TO {}", "REVOKE SELECT ON themes FROM {}"),
        ("REVOKE INSERT ON event_participants FROM {}", "GRANT INSERT ON event_participants TO {}"),
        ("REVOKE UPDATE ON entity_state FROM {}", "GRANT UPDATE ON entity_state TO {}"),
    ):
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL(grant).format(sql.Identifier(role)))
        try:
            with pytest.raises(WorkerConfigurationError, match="allow-list"):
                check.check_runtime_role()
        finally:
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute(sql.SQL(undo).format(sql.Identifier(role)))
    check.check_runtime_role()


# ---- 10: money ----------------------------------------------------------------------------------

def test_a_chapter_is_paid_within_the_allowance_admitted_whole(hosted_app, readers, worker_dsn,
                                                              database, monkeypatch):
    """The break it catches: extraction paid outside the reader's allowance or under other
    purposes than local mode's phase names; a chapter refused half-way by the day cap once its
    first call was admitted (its paid calls thrown away, and paid again the next day); or a
    reader already at the cap spending anything at all."""
    _client, _tokens, app = hosted_app
    client = extraction_client()
    extractor = Extractor(client, monkeypatch, usage=(500, 200))   # $0.0015 a call at terra
    a, b = readers["a"], readers["b"]
    for owner in (a, b):
        update_limits(database, owner, {"max_spend_usd_per_day": Decimal("0.001")})
    a_book = hosted_book(hosted_app, "a", harbour_book())
    b_book = hosted_book(hosted_app, "b", harbour_book())

    # Checked call by call (the plain catalog), the review's settled cost reaches the cap and
    # the chapter's next call is refused part-way: the case this is about.
    monkeypatch.setattr(story_memory_module, "ChapterCatalog", HostedCompanionCatalog)
    with pytest.raises(CostCeilingExceeded):
        remember(app, worker_dsn, b, b_book, client, 1)
    assert memory_of(database, b)["ingested_chapters"] == []
    monkeypatch.setattr(story_memory_module, "ChapterCatalog", ChapterCatalog)

    # Admitted whole: the same chapter for A is finished, past the cap by that one chapter.
    assert remember(app, worker_dsn, a, a_book, client, 1) is True
    ledger = settled_once(database, a)
    assert {row["purpose"] for row in ledger} == {"extraction", "extraction-review"}
    assert all(row["book_id"] == uuid.UUID(a_book) and row["chapter_ordinal"] == 1
               for row in ledger)
    spent = sum(row["usd"] for row in ledger)
    assert spent > Decimal("0.001")
    reservations, _ledger = spend_of(database, a)
    # The stub embedder's free calls are reserved like any other and released at zero.
    assert {(row["purpose"], row["state"]) for row in reservations} == {
        ("embedding", "released"), ("extraction", "settled"), ("extraction-review", "settled")}
    assert {row["phase"] for row in reservations} == {"synthesis"}

    # At the cap: the next chapter is refused at its first call and spends nothing.
    calls = list(extractor.calls)
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM jobs WHERE owner_id=%s AND state='pending'", (a,))
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("1")})
    held = claimed(app, worker_dsn, a, a_book)
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("0.001")})
    before = spend_of(database, a)
    with pytest.raises(CostCeilingExceeded):
        remember(app, worker_dsn, a, a_book, client, 2, claim=held)
    assert spend_of(database, a) == before and extractor.calls == calls
