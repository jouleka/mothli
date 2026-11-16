"""Chapter notes and durable companion jobs in hosted mode (ADR 0041 part 6), on real PostgreSQL.

A finished chapter (a page, "I've read this", a reply) adds one waiting ``chapter_notes`` job
through the RLS-enforced tenant role; the worker claims it with its own BYPASSRLS role and the
dispatcher runs the unchanged ``build_missing`` through the tenant role, bound to the claimed
owner. Proven here: notes end to end and their use in a reply; one waiting job however often it
is asked for; a refused provider account pauses every reader's notes, recorded once; a capped
reader waits for the next UTC day while others go on; a lease lost mid-run stores nothing twice;
a restarted worker resumes from rows alone; two readers; the roles' exact grants; and the
reader's job routes and upload quota left to extraction. Stand-in writer and Jev only.
"""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.companion.jobs import ChapterNotesQueue, CompanionJobs
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import PostgresWorkerRepository, WorkerConfigurationError
from app.hosted.limits import update_limits
from app.hosted.tenant.grants import TENANT_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.hosted.worker import DurableWorker
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_companion_stream_postgres import (
    CHAT,
    Reader,
    StandIns,
    events_of,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)
from tests.hosted import test_jobs_postgres as jobs_gate

pytestmark = pytest.mark.postgres

# The worker role, granted exactly the reviewed allow-list (grants.py).
worker_dsn = jobs_gate.worker_dsn
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
lamp_book = read_map_gate.lamp_book
line = read_map_gate.line
page = read_map_gate.page
upload = read_map_gate.upload

NOTES = {**CHAT, "reader_story_notes": True}
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture()
def notes_app(tenant_dsn: str, readers: dict, tmp_path):
    from datetime import UTC, datetime

    from app.main import create_app
    from tests.hosted.test_tenant_postgres import UnusedOIDCClient

    tokens, sessions = read_map_gate.signed_in(readers)
    app = create_app(
        read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects", **NOTES),
        auth_repository=sessions, oidc_client=UnusedOIDCClient(),
        auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    with TestClient(app, base_url="https://reader.example") as client:
        yield client, tokens, app


def reader(notes_app, readers, label="a") -> Reader:
    client, tokens, _app = notes_app
    return Reader(client, tokens, label, readers[label])


# ---- stand-in models -------------------------------------------------------------------------

def written(ordinal: int, label: str = "first") -> str:
    """What the stand-in writer writes for a chapter: two true sentences (which two depends on
    ``label``) and one the checker doubts and the second look drops."""
    first = 2 if label == "first" else 4
    return f"{line(ordinal, first)} {line(ordinal, first + 1)} Vex later sold it abroad."


class NotesModels(StandIns):
    """The stream tests' stand-ins, also writing chapter notes: for a prompt holding CHAPTER
    TEXT the writer returns ``written(ordinal, label)``. ``notes_hook`` runs during a notes call,
    before it returns."""

    def __init__(self, client, monkeypatch, *, notes_hook=None, **options):
        options.setdefault("unsupported", ["abroad"])
        super().__init__(client, monkeypatch, **options)
        self.notes_prompts, self.notes_hook, self.label = [], notes_hook, "first"

    def complete(self, system, user, tier="cheap", schema=None):
        if schema is None and "CHAPTER TEXT" in user:
            ordinal = int(re.search(r"CHAPTER TEXT \(Part (\d+)\)", user)[1])
            label = self.label
            with self._lock:
                self.notes_prompts.append(user)
            if self.notes_hook:
                self.notes_hook()
            return written(ordinal, label), {"in": 100, "out": 40}
        return super().complete(system, user, tier=tier, schema=schema)


# ---- the worker, its rows and the dispatcher ---------------------------------------------------

def dispatcher(app, *, lease_for=timedelta(seconds=60)) -> CompanionJobs:
    """The worker's handler as ``from_environment`` builds it, from the app's own pieces."""
    return CompanionJobs(tenant_dsn=app.state.companion_dsn, settings=app.state.settings,
                         client=app.state.platform_client, storage=app.state.object_storage,
                         sources=app.state.companion_sources, lease_for=lease_for)


def worker(app, dsn: str, worker_id: str = "notes-worker") -> DurableWorker:
    return DurableWorker(PostgresWorkerRepository(dsn), dispatcher(app), worker_id,
                         lease_for=timedelta(seconds=60), retry_after=timedelta(seconds=15))


def drain(work: DurableWorker, most: int = 10) -> int:
    ran = 0
    while ran < most and work.run_once():
        ran += 1
    return ran


def rows(dsn: str, query: str, *params) -> list[dict]:
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute(query, params).fetchall()


def jobs_of(dsn: str, owner: uuid.UUID, kind: str = "chapter_notes") -> list[dict]:
    return rows(dsn, "SELECT * FROM jobs WHERE owner_id=%s AND kind=%s ORDER BY created_at,id",
                owner, kind)


def notes_of(dsn: str, owner: uuid.UUID) -> list[dict]:
    return rows(dsn, "SELECT book_id,book_incarnation,atom,source_hash,version,sentences,dropped,"
                     "created_at FROM chapter_notes WHERE owner_id=%s ORDER BY book_id,atom", owner)


def spend_of(dsn: str, owner: uuid.UUID) -> tuple[list, list]:
    return (rows(dsn, "SELECT * FROM cost_reservations WHERE owner_id=%s ORDER BY created_at,id",
                 owner),
            rows(dsn, "SELECT * FROM cost_ledger WHERE owner_id=%s ORDER BY recorded_at,id", owner))


def kept(ordinal: int, label: str = "first") -> list[str]:
    first = 2 if label == "first" else 4
    return [line(ordinal, first), line(ordinal, first + 1)]


def finish_chapter_two(someone: Reader, book: str) -> None:
    """One page from the end of chapter 1 to the end of chapter 2: chapter 2 read from its
    heading on, so the page finishes it."""
    response = someone.call("PUT", f"/api/books/{book}/read-page",
                            page(1, 60, 60, to_chapter=2))
    assert response.status_code == 200, response.text


def expire_lease(database: str, owner: uuid.UUID) -> None:
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE job_attempts SET leased_at=now()-interval '2 seconds',"
                     "lease_expires_at=now()-interval '1 second' "
                     "WHERE owner_id=%s AND finished_at IS NULL", (owner,))


def opened(notes_app, readers, label="a") -> tuple[Reader, str]:
    """A reader with the lamp book and chapter 1 marked read (which asks for its notes)."""
    someone = reader(notes_app, readers, label)
    book = upload(someone.client, someone.tokens, label, lamp_book())
    someone.mark(book, [1])
    return someone, book


# ---- end to end ------------------------------------------------------------------------------

def test_a_finished_chapter_gets_checked_notes_through_a_durable_job(notes_app, readers, database,
                                                                      worker_dsn, monkeypatch):
    """The break it catches: hosted notes that are not asked for where local mode asks (a
    finished page, a chapter marked read, a reply), written from text past the reader's place,
    kept without their checks, stored or charged to the wrong reader, or not used in a reply."""
    _client, _tokens, app = notes_app
    models = NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a, book = opened(notes_app, readers)
    [job] = jobs_of(database, a.owner)
    assert (job["state"], str(job["book_id"]), job["credential_id"], job["payload_metadata"],
            job["priority"]) == ("pending", book, None, {}, 10)

    assert drain(worker(app, worker_dsn)) == 1
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded"]
    [note] = notes_of(database, a.owner)
    assert (note["atom"], note["sentences"], note["dropped"]) == (1, kept(1), 1)
    assert note["version"] == "chapter-notes-v1" and re.fullmatch("[0-9a-f]{16}", note["source_hash"])
    # The writer was given chapter 1 and nothing of the chapters not read.
    [prompt] = models.notes_prompts
    assert line(1, 40) in prompt and "in part 2" not in prompt and "in part 3" not in prompt
    reservations, ledger = spend_of(database, a.owner)
    assert {row["purpose"] for row in ledger} >= {"notes-write", "notes-sentence-check"}
    assert all(row["book_id"] == uuid.UUID(book) for row in ledger)
    assert not [row for row in reservations if row["state"] == "reserved"]

    # Reading into chapter 2 asks for nothing until it is finished; finishing it asks again.
    a.read(book, 2, 1, 5)
    assert len(jobs_of(database, a.owner)) == 1
    finish_chapter_two(a, book)
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded", "pending"]
    drain(worker(app, worker_dsn))
    assert [(n["atom"], n["sentences"]) for n in notes_of(database, a.owner)] == [
        (1, kept(1)), (2, kept(2))]
    assert "in part 3" not in models.notes_prompts[-1]

    # A reply uses the notes of chapters read in full, and asks for notes after replying.
    events = a.ask(book, "What has happened so far?")
    assert sentences(events) == [line(1, 2)]
    assert "YOUR NOTES ON THE CHAPTERS READ IN FULL" in models.prompts[-1]
    assert line(2, 3) in models.prompts[-1]
    assert [row["state"] for row in jobs_of(database, a.owner)][-1] == "pending"
    # "I haven't read chapter 2": its notes are no longer given to a reply.
    a.mark(book, [2], read=False)
    a.ask(book, "What has happened so far?")
    assert line(2, 3) not in models.prompts[-1] and line(1, 3) in models.prompts[-1]


def test_a_second_ensure_while_a_job_waits_adds_no_job(notes_app, readers, database, worker_dsn,
                                                       monkeypatch):
    """The break it catches: every page turn and reply adding a job (a queue that grows with
    reading), or a second job for the same book running beside the first and paying twice."""
    _client, _tokens, app = notes_app
    NotesModels(app.state.platform_client, monkeypatch)
    a, book = opened(notes_app, readers)
    queue = ChapterNotesQueue(app.state.companion_dsn, OwnerId(a.owner))
    assert queue.ensure(book) is False
    a.mark(book, [1])
    finish_chapter_two(a, book)                          # asks again
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"]

    # While it runs, one more may wait, and only one.
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="first", lease_for=timedelta(seconds=60),
                                  kinds=frozenset({"chapter_notes"}))
    assert claim is not None and repository.start(claim)
    assert queue.ensure(book) is True
    assert queue.ensure(book) is False
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["running", "pending"]
    # The waiting one is not claimed while the first runs.
    assert repository.claim_next(worker_id="second", lease_for=timedelta(seconds=60)) is None
    # A retry of the running one, with another already waiting, gives way to it.
    from app.hosted.jobs import SanitizedFailure

    repository.fail(claim, failure=SanitizedFailure("internal_error", retryable=True),
                    retry_after=timedelta(0))
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["cancelled", "pending"]
    assert drain(worker(app, worker_dsn)) == 1
    assert [(n["atom"], n["sentences"]) for n in notes_of(database, a.owner)] == [
        (1, kept(1)), (2, kept(2))]


# ---- the platform's provider refused: a pause for everyone, recorded once ------------------------

def test_a_refused_provider_pauses_every_readers_notes_and_is_recorded_once(
        notes_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: one reader's job meeting the platform's refused checker account
    while the other readers' jobs go on paying the writer and being refused the same way, a
    pause per reader or per job, a pause that does not hold, or one owner's refusal writing to
    or charging the other."""
    from tests.test_jev import provider

    _client, _tokens, app = notes_app
    client = app.state.platform_client
    a, book_a = opened(notes_app, readers, "a")
    b, book_b = opened(notes_app, readers, "b")
    refused = provider(402)
    models = NotesModels(client, monkeypatch, jev=refused)
    work = worker(app, worker_dsn)
    try:
        assert work.run_once()                            # A's job, the first asked for
    finally:
        refused.close()
    [job_a] = jobs_of(database, a.owner)
    assert (job_a["state"], job_a["sanitized_error"]) == (
        "pending", {"code": "provider_rejected", "retryable": True})
    pauses = rows(database, "SELECT provider,reason,paused_until-now() AS left FROM provider_pauses")
    assert [(p["provider"], p["reason"]) for p in pauses] == [("typesafe", "no_credits")]
    assert timedelta(minutes=4) < pauses[0]["left"] <= timedelta(minutes=5)
    assert notes_of(database, a.owner) == []
    assert models.writes == 0 and len(models.notes_prompts) == 1

    # Every reader waits, the one who met the refusal and the one who did not.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE kind='chapter_notes'")
    assert work.run_once() is False
    [job_b] = jobs_of(database, b.owner)
    assert (job_b["state"], job_b["attempt_count"]) == ("pending", 0)
    assert spend_of(database, b.owner) == ([], []) and notes_of(database, b.owner) == []
    assert len(models.notes_prompts) == 1
    assert len(rows(database, "SELECT 1 FROM provider_pauses")) == 1
    audit = rows(database, "SELECT owner_id FROM audit_events WHERE action='provider.pause'")
    assert [row["owner_id"] for row in audit] == [a.owner]

    # When the pause ends, both go on, each on its own rows.
    NotesModels(client, monkeypatch)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE provider_pauses SET paused_until=now()-interval '1 second'")
    assert drain(work) == 2
    for owner, book in ((a.owner, book_a), (b.owner, book_b)):
        assert [row["state"] for row in jobs_of(database, owner)] == ["succeeded"]
        assert [(str(n["book_id"]), n["atom"], n["sentences"]) for n in notes_of(database, owner)] == [
            (book, 1, kept(1))]
        assert all(row["book_id"] == uuid.UUID(book) for row in spend_of(database, owner)[1])


def test_a_capped_reader_waits_for_the_next_day_and_others_go_on(notes_app, readers, database,
                                                                  worker_dsn, monkeypatch):
    """The break it catches: a reader at their day's allowance whose notes are claimed again and
    again (each claim a refused call or a retry burnt), or who holds up another reader's notes."""
    _client, _tokens, app = notes_app
    a, book_a = opened(notes_app, readers, "a")
    b, book_b = opened(notes_app, readers, "b")
    update_limits(database, a.owner, {"max_spend_usd_per_day": Decimal("0.05")})
    catalog = HostedCompanionCatalog(database, OwnerId(a.owner))
    held = catalog.reserve_cost(book_a, phase="stream-reply", model="gpt-6.1-terra",
                                input_tokens=10, output_tokens=10, usd=0.05, max_input_tokens=1,
                                max_output_tokens=1, max_usd=0.0)
    catalog.settle_cost(book_a, held)                      # exactly at the cap
    models = NotesModels(app.state.platform_client, monkeypatch)
    assert drain(worker(app, worker_dsn)) == 1
    [job_a] = jobs_of(database, a.owner)
    tomorrow = rows(database, "SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') + interval "
                              "'1 day') AT TIME ZONE 'UTC' AS at")[0]["at"]
    assert (job_a["state"], job_a["attempt_count"], job_a["run_after"]) == ("pending", 0, tomorrow)
    assert notes_of(database, a.owner) == [] and len(models.notes_prompts) == 1
    assert [row["state"] for row in jobs_of(database, b.owner)] == ["succeeded"]
    assert [n["sentences"] for n in notes_of(database, b.owner)] == [kept(1)]


# ---- a lost lease, a restart --------------------------------------------------------------------

def test_a_lease_lost_mid_run_does_not_store_notes_twice(notes_app, readers, database, worker_dsn,
                                                         monkeypatch):
    """The break it catches: a worker whose lease ran out mid-run (another worker has since
    taken the job and stored its notes) going on to reserve more money and to store its own
    notes over the ones kept. At worst a model call is repeated; notes are stored once."""
    _client, _tokens, app = notes_app
    a, book = opened(notes_app, readers)
    first, second = worker(app, worker_dsn, "first"), worker(app, worker_dsn, "second")
    after_second = {}

    def lose_the_lease_and_let_another_worker_finish():
        if after_second:
            return
        after_second["started"] = True
        expire_lease(database, a.owner)
        models.label = "second"
        try:
            after_second["ran"] = second.run_once()
        finally:
            models.label = "first"
        after_second["notes"] = notes_of(database, a.owner)
        after_second["reservations"] = len(spend_of(database, a.owner)[0])

    models = NotesModels(app.state.platform_client, monkeypatch,
                         notes_hook=lose_the_lease_and_let_another_worker_finish)
    assert first.run_once()
    assert after_second["ran"] is True and after_second["notes"]
    # The second worker's notes, stored once, untouched by the first worker coming back.
    [note] = notes_of(database, a.owner)
    assert note["sentences"] == kept(1, "second")
    assert notes_of(database, a.owner) == after_second["notes"]
    assert len(models.notes_prompts) == 2                 # the repeated model call, and no more
    reservations, _ledger = spend_of(database, a.owner)
    assert len(reservations) == after_second["reservations"]
    assert not [row for row in reservations if row["state"] == "reserved"]
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("succeeded", 2)
    attempts = rows(database, "SELECT attempt_no,worker_id,outcome FROM job_attempts "
                              "WHERE owner_id=%s ORDER BY attempt_no", a.owner)
    assert [(row["worker_id"], row["outcome"]) for row in attempts] == [
        ("first", "expired"), ("second", "succeeded")]


def test_notes_already_kept_for_the_same_text_are_never_replaced(notes_app, readers, database):
    """The adapter's own guard: a second store of the same chapter, text and version (a job
    that lost its lease after the lease check) changes nothing; new text or a new version does."""
    _client, _tokens, app = notes_app
    a, book = opened(notes_app, readers)
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    catalog.set_chapter_notes(book, 1, source_hash="0" * 16, version="chapter-notes-v1",
                              sentences=["Kept."], dropped=0)
    stored = notes_of(database, a.owner)
    catalog.set_chapter_notes(book, 1, source_hash="0" * 16, version="chapter-notes-v1",
                              sentences=["Written twice."], dropped=2)
    assert notes_of(database, a.owner) == stored
    catalog.set_chapter_notes(book, 1, source_hash="1" * 16, version="chapter-notes-v1",
                              sentences=["New text."], dropped=0)
    assert catalog.get_chapter_notes(book) == [
        {"atom": 1, "source_hash": "1" * 16, "version": "chapter-notes-v1",
         "sentences": ["New text."], "dropped": 0}]


def test_a_restarted_worker_resumes_from_rows_alone(notes_app, readers, database, worker_dsn,
                                                    monkeypatch):
    """The break it catches: queued or running work, or a pause, kept in a worker's memory and
    lost when it stops. Here the first worker claims and starts a job and is gone; a new worker,
    with a new dispatcher and nothing carried over, waits out the recorded pause and finishes."""
    _client, _tokens, app = notes_app
    a, book = opened(notes_app, readers)
    gone = PostgresWorkerRepository(worker_dsn)
    claim = gone.claim_next(worker_id="gone", lease_for=timedelta(seconds=60),
                            kinds=frozenset({"chapter_notes"}))
    assert claim is not None and gone.start(claim)
    with psycopg.connect(database) as conn:
        conn.execute("INSERT INTO provider_pauses (provider,reason,paused_until) "
                     "VALUES ('typesafe','no_credits',now()+interval '5 minutes')")
    expire_lease(database, a.owner)
    del gone, claim

    models = NotesModels(app.state.platform_client, monkeypatch)
    restarted = worker(app, worker_dsn, "restarted")
    assert restarted.run_once() is False                  # the pause is a row: it still holds
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("pending", 1)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE provider_pauses SET paused_until=now()-interval '1 second'")
    assert restarted.run_once()
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("succeeded", 2)
    assert [n["sentences"] for n in notes_of(database, a.owner)] == [kept(1)]
    assert len(models.notes_prompts) == 1


# ---- two readers -------------------------------------------------------------------------------

def test_second_reader_cannot_see_trigger_or_change_first_readers_notes_or_jobs(
        notes_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The break it catches: a second reader learning that the first reader's book exists,
    adding (and so paying for) a job on it, reading or changing its notes or jobs through the
    routes, through the database role (row-level security alone) or through an adapter bound to
    them on a connection where row-level security does not apply (its own predicates alone)."""
    _client, _tokens, app = notes_app
    models = NotesModels(app.state.platform_client, monkeypatch)
    a, book_a = opened(notes_app, readers, "a")
    drain(worker(app, worker_dsn))
    finish_chapter_two(a, book_a)                          # one more job, left waiting
    jobs_a, notes_a = jobs_of(database, a.owner), notes_of(database, a.owner)
    assert [row["state"] for row in jobs_a] == ["succeeded", "pending"] and notes_a

    b = reader(notes_app, readers, "b")
    book_b = upload(b.client, b.tokens, "b", lamp_book())
    missing = uuid.uuid4()
    for method, suffix, body in (("PUT", "/read-chapters", {"atoms": [1, 2, 3], "read": True}),
                                 ("PUT", "/read-page", page(2, 1, 60)),
                                 ("POST", "/ask/stream", {"question": "What happened?",
                                                          "read_frontier": b.point(book_b)})):
        hidden = b.call(method, f"/api/books/{book_a}{suffix}", body)
        absent = b.call(method, f"/api/books/{missing}{suffix}", body)
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json()), suffix
        assert (hidden.status_code, hidden.json()) == (404, {"detail": "unknown book"}), suffix
    b.become()
    listed = b.client.get("/api/jobs").json()
    assert {str(job["book_id"]) for job in listed} == {book_b}
    for job in jobs_a:
        assert b.client.get(f"/api/jobs/{job['id']}").status_code == 404
        assert b.call("POST", f"/api/jobs/{job['id']}/cancel").status_code == 404
    assert jobs_of(database, b.owner) == []
    assert jobs_of(database, a.owner) == jobs_a and notes_of(database, a.owner) == notes_a

    # Row-level security alone: B's role setting, and no predicate naming any owner.
    incarnation = rows(database, "SELECT incarnation FROM books WHERE id=%s", book_a)[0]["incarnation"]
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b.owner),))
        assert conn.execute("SELECT count(*) FROM chapter_notes").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind='chapter_notes'"
                            ).fetchone()[0] == 0
        assert conn.execute("DELETE FROM chapter_notes").rowcount == 0
        assert conn.execute("UPDATE chapter_notes SET dropped=9").rowcount == 0
        assert conn.execute("UPDATE jobs SET priority=99 WHERE kind='chapter_notes'"
                            ).rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """INSERT INTO chapter_notes
                     (owner_id,book_id,book_incarnation,atom,source_hash,version,sentences,dropped)
                   VALUES (%s,%s,%s,3,%s,'v','["Planted."]',0)""",
                (a.owner, book_a, incarnation, "0" * 16))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,idempotency_key)
                   VALUES (%s,%s,%s,'chapter_notes','chapter-notes:planted')""",
                (a.owner, book_a, incarnation))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM provider_pauses")

    # The adapters' own predicates alone: bound to B on a superuser connection.
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    other = HostedCompanionCatalog(database, OwnerId(b.owner))
    assert other.get_chapter_notes(book_a) == []
    with pytest.raises(LookupError):
        other.set_chapter_notes(book_a, 3, source_hash="0" * 16, version="chapter-notes-v1",
                                sentences=["Planted."], dropped=0)
    assert ChapterNotesQueue(database, OwnerId(b.owner)).ensure(book_a) is False
    assert HostedCompanionCatalog(database, OwnerId(a.owner)).get_chapter_notes(book_a)
    assert jobs_of(database, b.owner) == []
    assert jobs_of(database, a.owner) == jobs_a and notes_of(database, a.owner) == notes_a
    assert len(models.notes_prompts) == 1


# ---- the reader's job routes and upload quota ---------------------------------------------------

def test_waiting_notes_neither_block_an_upload_nor_show_as_the_readers_jobs(notes_app, readers,
                                                                            database):
    """The break it catches: a busy companion filling the reader's job quota so a book cannot be
    uploaded, or the server's own background jobs listed, shown or cancellable on the reader's
    extraction screen."""
    a, _book = opened(notes_app, readers)
    update_limits(database, a.owner, {"max_active_jobs": 2})
    [notes_job] = jobs_of(database, a.owner)
    assert len(jobs_of(database, a.owner, "ingest_book")) == 1
    second = upload(a.client, a.tokens, "a", lamp_book())          # 2 extraction jobs, 1 notes
    assert second
    a.become()
    listed = a.client.get("/api/jobs").json()
    assert len(listed) == 2 and {job["kind"] for job in listed} == {"ingest_book"}
    assert a.client.get("/api/limits").json()["active_jobs"] == 2
    assert a.client.get(f"/api/jobs/{notes_job['id']}").status_code == 404
    assert a.call("POST", f"/api/jobs/{notes_job['id']}/cancel").status_code == 404
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"]


# ---- grants and startup ----------------------------------------------------------------------

def held_privileges(database: str, role: str, table: str) -> set[str]:
    with psycopg.connect(database, autocommit=True) as conn:
        return {privilege for privilege in ALL_PRIVILEGES
                if conn.execute("SELECT has_table_privilege(%s, %s, %s)",
                                (role, f"public.{table}", privilege)).fetchone()[0]}


def test_tenant_and_worker_roles_hold_exactly_the_part_6_grants(tenant_dsn: str, worker_dsn: str,
                                                                database: str) -> None:
    """The break it catches: the reader role able to read or write the platform's pause (or
    unable to keep notes), or the worker role holding a privilege on the pause nobody reviewed.
    Both startup checks pass with exactly these grants and refuse one more or one fewer."""
    tenant = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    worker_role = conninfo.conninfo_to_dict(worker_dsn)["user"]
    assert TENANT_TABLE_PRIVILEGES["chapter_notes"] == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert "provider_pauses" not in TENANT_TABLE_PRIVILEGES
    assert WORKER_TABLE_PRIVILEGES["provider_pauses"] == {"SELECT", "INSERT", "UPDATE"}
    assert "chapter_notes" not in WORKER_TABLE_PRIVILEGES
    assert held_privileges(database, tenant, "chapter_notes") == TENANT_TABLE_PRIVILEGES["chapter_notes"]
    assert held_privileges(database, tenant, "provider_pauses") == set()
    assert held_privileges(database, worker_role, "provider_pauses") == {"SELECT", "INSERT", "UPDATE"}
    assert held_privileges(database, worker_role, "chapter_notes") == set()
    tenant_check = PostgresTenantRepository(tenant_dsn)
    worker_check = PostgresWorkerRepository(worker_dsn)
    asyncio.run(tenant_check.check_runtime_role())
    worker_check.check_runtime_role()

    def refused(grant: str, undo: str, role: str, check, error) -> None:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL(grant).format(sql.Identifier(role)))
        try:
            with pytest.raises(error, match="allow-list"):
                check()
        finally:
            with psycopg.connect(database, autocommit=True) as conn:
                conn.execute(sql.SQL(undo).format(sql.Identifier(role)))

    def tenant_runs():
        asyncio.run(tenant_check.check_runtime_role())

    for grant, undo in (
        ("GRANT SELECT ON provider_pauses TO {}", "REVOKE SELECT ON provider_pauses FROM {}"),
        ("GRANT TRUNCATE ON chapter_notes TO {}", "REVOKE TRUNCATE ON chapter_notes FROM {}"),
        ("REVOKE UPDATE ON chapter_notes FROM {}", "GRANT UPDATE ON chapter_notes TO {}"),
    ):
        refused(grant, undo, tenant, tenant_runs, AuthConfigurationError)
    for grant, undo in (
        ("GRANT DELETE ON provider_pauses TO {}", "REVOKE DELETE ON provider_pauses FROM {}"),
        ("REVOKE INSERT ON provider_pauses FROM {}", "GRANT INSERT ON provider_pauses TO {}"),
        ("GRANT SELECT ON chapter_notes TO {}", "REVOKE SELECT ON chapter_notes FROM {}"),
    ):
        refused(grant, undo, worker_role, worker_check.check_runtime_role,
                WorkerConfigurationError)
    asyncio.run(tenant_check.check_runtime_role())
    worker_check.check_runtime_role()


def test_a_companion_job_is_well_formed_or_refused_by_the_schema(notes_app, readers, database):
    """The break it catches: a companion job carrying a credential (the platform pays), a notes
    job naming no book, or a second job waiting for the same book inserted around the queue."""
    a, book = opened(notes_app, readers)
    incarnation = rows(database, "SELECT incarnation FROM books WHERE id=%s", book)[0]["incarnation"]
    with psycopg.connect(database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO jobs (owner_id,kind,idempotency_key) "
                         "VALUES (%s,'chapter_notes','chapter-notes:no-book')", (a.owner,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,"
                         "idempotency_key) VALUES (%s,%s,%s,'chapter_notes','chapter-notes:twin')",
                         (a.owner, book, incarnation))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO jobs (owner_id,kind,idempotency_key) "
                         "VALUES (%s,'something_else','x:1')", (a.owner,))
    assert len(jobs_of(database, a.owner)) == 1


def test_stream_events_are_unchanged_by_the_notes_queue(notes_app, readers, database,
                                                        monkeypatch):
    """The reply is answered before its notes are asked for, and an unreachable queue never
    breaks a reply or a page report (as locally, the next one asks again)."""
    _client, _tokens, app = notes_app
    NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a, book = opened(notes_app, readers)
    monkeypatch.setattr(ChapterNotesQueue, "_transaction",
                        lambda self: (_ for _ in ()).throw(psycopg.OperationalError("down")))
    events = events_of(a.stream(book))
    assert sentences(events) == [line(1, 2)] and events[-1]["type"] == "done"
    a.mark(book, [2])
    assert len(jobs_of(database, a.owner)) == 1


def test_a_deleted_books_notes_are_never_written(notes_app, readers, database, worker_dsn,
                                                 monkeypatch) -> None:
    """The break it catches: notes written (and paid for) for a book the reader has removed,
    from a job that waited or one that was already running when the book went."""
    _client, _tokens, app = notes_app
    models = NotesModels(app.state.platform_client, monkeypatch)
    a, book = opened(notes_app, readers)
    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["cancelled"]
    assert drain(worker(app, worker_dsn)) == 0

    # A job already running when its book goes stops at its next paid call or store.
    other = upload(a.client, a.tokens, "a", lamp_book())
    a.mark(other, [1])

    def remove_the_book():
        if not models.notes_prompts[1:]:
            assert a.call("DELETE", f"/api/books/{other}").status_code == 204

    models.notes_hook = remove_the_book
    assert worker(app, worker_dsn).run_once()
    assert notes_of(database, a.owner) == []
    reservations, _ledger = spend_of(database, a.owner)
    assert not [row for row in reservations if row["state"] == "reserved"]
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["cancelled", "cancelled"]


def test_a_companion_worker_leaves_extraction_jobs_alone(notes_app, readers, database,
                                                        worker_dsn) -> None:
    """The break it catches: the companion dispatcher's worker claiming a waiting extraction job,
    which it cannot run and would fail."""
    a = reader(notes_app, readers)
    upload(a.client, a.tokens, "a", lamp_book())
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET state='pending' WHERE kind='ingest_book'")
    repository = PostgresWorkerRepository(worker_dsn)
    assert repository.claim_next(worker_id="notes", lease_for=timedelta(seconds=30),
                                 kinds=frozenset({"chapter_notes"})) is None
    claimed = repository.claim_next(worker_id="any", lease_for=timedelta(seconds=30))
    assert claimed is not None and claimed.kind == "ingest_book"


def test_the_dispatcher_refuses_to_start_without_its_settings(monkeypatch, tmp_path) -> None:
    """The break it catches: a worker started in local mode, or with chapter notes off, that
    would claim companion jobs it cannot run."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(WorkerConfigurationError, match="hosted mode only"):
        CompanionJobs.from_environment()
    with pytest.raises(WorkerConfigurationError, match="chapter notes only"):
        CompanionJobs(tenant_dsn="postgresql://unused.invalid/db", settings=None, client=None,
                      storage=None, sources=None, kinds=frozenset({"recollect"}))


def test_queue_ensure_is_safe_from_many_threads(notes_app, readers, database) -> None:
    """Many page reports at once for one book add one waiting job."""
    _client, _tokens, app = notes_app
    a, book = opened(notes_app, readers)
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM jobs WHERE kind='chapter_notes'")
    queue = ChapterNotesQueue(app.state.companion_dsn, OwnerId(a.owner))
    results = []
    threads = [threading.Thread(target=lambda: results.append(queue.ensure(book)))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"]
