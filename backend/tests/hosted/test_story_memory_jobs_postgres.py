"""Story memory as durable jobs in hosted mode (ADR 0042 part 3), against real PostgreSQL.

A finished chapter (a page that finishes it, "I've read this") adds one waiting ``story_memory``
job through the RLS-enforced tenant role; the worker claims it with its own role and the
companion dispatcher runs ``remember_chapter`` for the next chapter the reader has finished, one
chapter per run, then asks for the next. Proven here: one waiting and one running job per owner
and book; the target is the read map, never a chapter counter pushed ahead, and marking chapters
not read stops queued work at the next chapter and keeps what was built; a capped reader waits
whole for the next UTC day while others go on, and the platform's day bounds everyone; a chapter
that fails on two runs (the review's rejection, or a person tied to themselves) is kept as an
unremembered chapter and the book goes on, never looser than local mode on the chat's spoiler
probes; a refused writer pauses every reader and an outage retries, neither counted against the
chapter; a lost lease, a removed book and a dead worker; two readers; erasure, removal and the
copy of one's data with these jobs present; and the settings that switch it all off. Stand-in
writer, checker and Jev only: nothing is called and nothing paid.

ADR 0044 step 3: every run builds into the edition of the reader's file (the notes account's book
with the same two fingerprints), made on its first chapter, and the reader owns no story memory
row; what a reader is shown comes through the door, up to their place. Where two readers here
must not share notes, their files differ by their title alone. Two readers of one file, the
build slot and failures counted per edition are ``test_story_memory_editions_postgres.py``.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.eval.spoiler_gate import score_recap, unread_future_names
from app.hosted.companion.jobs import CompanionJobs, StoryMemoryQueue, story_memory_failure
from app.hosted.companion.later_names import rebuild_name_words
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.spend import set_spend_limits, spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import PostgresWorkerRepository
from app.hosted.limits import update_limits
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_account
from app.hosted.tenant.grants import purge_grants
from app.hosted.tenant.models import OwnerId
from app.hosted.worker import DurableWorker
from app.ingest.extraction.validation import ExtractionGroundingRejected
from app.known_names import same_person_names
from app.llm.client import ProviderUnavailable
from app.main import create_app
from app.unicode_text import word_tokens
from tests.api._epub import epub_ncx
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_account_export_postgres import copy_of
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    CHAT,
    Reader,
    StandIns,
    restore_caps,
    sentences,
)
from tests.hosted.test_story_memory_postgres import (
    book_memory,
    edition_memory,
    edition_of,
    memory_of,
    no_memory_of_their_own,
    notes_account,
    notes_spend,
    settled_once,
    spend_of,
)
from tests.hosted.test_tenant_postgres import UnusedOIDCClient
from tests.ingest._grounding import canned_grounding_verdict
from tests.parity.test_story_memory_parity import _person, extraction_client, hosted_atoms, local_book

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
worker_dsn = jobs_gate.worker_dsn
line = read_map_gate.line
upload = read_map_gate.upload

MEMORY = {**CHAT, "hosted_story_memory": True}
GREETING = "Corvus greeted Vex at the quay."


def lamps(title: str = "The Lamp Book") -> bytes:
    """The chat tests' lamp book (Mara in part 1, Corvus in part 2, Vex in part 3, sixty lines
    each), with Corvus named once more at the end of part 3. Another ``title`` makes another file
    (so another edition, ADR 0044 step 3) with the very same parts; the same title made twice is
    one file (``epub_ncx`` stamps no clock)."""
    parts = [" ".join(line(n, i) for i in range(1, 61)) for n in (1, 2, 3)]
    parts[2] += " " + GREETING
    return epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}", text)
                     for n, text in enumerate(parts, start=1)],
                    title=title, author="Ada Writer")


OTHER_FILE = "The Lamp Book, Another Printing"


# What the stand-in writer extracts from each part. "the Lamplighter" is Vex's other name, first
# given in part 3; Corvus is first met in part 2 and named again in part 3.
EXTRACTIONS = {
    1: {"chapter_summary": "Mara moved the lamps.",
        "entities": [_person("Mara", state="moving lamps")], "relationships": [],
        "events": [{"summary": "Mara moved lamp 1.", "participants": ["Mara"]}],
        "themes": [{"name": "Light", "description": "Lamps are moved."}]},
    2: {"chapter_summary": "Corvus moved the lamps.",
        "entities": [_person("Corvus", state="moving lamps")], "relationships": [],
        "events": [{"summary": "Corvus moved lamp 1.", "participants": ["Corvus"]}],
        "themes": []},
    3: {"chapter_summary": "Vex moved the lamps and Corvus greeted him.",
        "entities": [_person("Vex", aliases=["the Lamplighter"]),
                     _person("Corvus", known=True)],
        "relationships": [{"src": "Corvus", "dst": "Vex", "rel_type": "social",
                           "label": "greets"}],
        "events": [{"summary": "Vex moved lamp 1.", "participants": ["Vex"]},
                   {"summary": "Corvus greeted Vex at the quay.",
                    "participants": ["Corvus", "Vex"]}],
        "themes": []},
}


class Lamps:
    """The stand-in writer and checker for extraction over the lamp book: the scripted extraction
    of the part the prompt holds, and an accepting review, except that the review rejects the
    parts in ``reject`` (a later plot development, as a real review would say of an extraction
    that imports one) and the writer ties Corvus to himself in the parts in ``self_relation``.
    ``fail`` maps (schema, part) to an exception raised once at that call; ``hook(schema, part)``
    runs during every call. Every call is counted as (schema, part)."""

    def __init__(self, client, monkeypatch, *, reject=(), self_relation=(), malformed=(),
                 fail=None, hook=None, usage=(120, 60)):
        self.calls, self.reject, self.self_relation = [], set(reject), set(self_relation)
        self.malformed = set(malformed)
        self.fail, self.hook, self.usage = dict(fail or {}), hook, usage
        monkeypatch.setattr(client, "complete", self.complete)

    def complete(self, system, user, *, tier, schema):
        part = int(re.search(r" in part (\d)\.", user)[1])
        self.calls.append((schema.__name__, part))
        if self.hook:
            self.hook(schema.__name__, part)
        failure = self.fail.pop((schema.__name__, part), None)
        if failure is not None:
            raise failure
        usage = {"in": self.usage[0], "out": self.usage[1]}
        if schema.__name__ == "ExtractionVerdict":
            return canned_grounding_verdict(user, references_future=part in self.reject), usage
        assert schema.__name__ == "Extraction", schema.__name__
        if part in self.malformed:
            return {"chapter_summary": "", "entities": "nobody"}, usage
        extraction = json.loads(json.dumps(EXTRACTIONS[part]))
        if part in self.self_relation:
            extraction["relationships"].append({"src": "Corvus", "dst": "Corvus",
                                                "rel_type": "social", "label": "talks to"})
        return extraction, usage

    def parts(self, schema="Extraction") -> list[int]:
        return [part for name, part in self.calls if name == schema]


# ---- the app, the worker and the rows ----------------------------------------------------------

def memory_app_for(tenant_dsn, readers, tmp_path, **settings):
    tokens, sessions = read_map_gate.signed_in(readers)
    app = create_app(read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects", **settings),
                     auth_repository=sessions, oidc_client=UnusedOIDCClient(),
                     auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
    return TestClient(app, base_url="https://reader.example"), tokens, app


@pytest.fixture()
def memory_app(tenant_dsn: str, readers: dict, tmp_path):
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **MEMORY)
    with client:
        yield client, tokens, app


def dispatcher(app, extraction, *, lease_for=timedelta(seconds=60)) -> CompanionJobs:
    """The worker's handler as ``from_environment`` builds it with HOSTED_STORY_MEMORY on."""
    return CompanionJobs(tenant_dsn=app.state.companion_dsn, settings=app.state.settings,
                         client=app.state.platform_client, storage=app.state.object_storage,
                         sources=app.state.companion_sources, kinds=frozenset({"story_memory"}),
                         extraction_client=extraction, lease_for=lease_for)


def worker(app, extraction, dsn: str, worker_id: str = "memory-worker") -> DurableWorker:
    return DurableWorker(PostgresWorkerRepository(dsn), dispatcher(app, extraction), worker_id,
                         lease_for=timedelta(seconds=60), retry_after=timedelta(0))


def drain(work: DurableWorker, most: int = 12) -> int:
    ran = 0
    while ran < most and work.run_once():
        ran += 1
    return ran


def rows(dsn: str, query: str, *params) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()


def jobs_of(dsn: str, owner: uuid.UUID, kind: str = "story_memory") -> list[dict]:
    return rows(dsn, f"SELECT * FROM jobs WHERE owner_id=%s AND kind=%s {jobs_gate.JOB_ORDER}",
                owner, kind)


def built(dsn: str, owner: uuid.UUID) -> list[tuple[int, str]]:
    """(ordinal, kind) of every chapter of the owner whose story memory is published."""
    return [(row["revealed_at"], row["kind"]) for row in rows(
        dsn, """SELECT chapter.revealed_at,chapter.kind FROM chapters AS chapter
                JOIN ingested_chapters AS receipt
                  ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                   =(chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                WHERE chapter.owner_id=%s ORDER BY chapter.revealed_at""", owner)]


def built_in(dsn: str, scope: tuple | None) -> list[tuple[int, str]]:
    """(ordinal, kind) of every published chapter of one (owner, book): an edition's."""
    if scope is None:
        return []
    return [(row["revealed_at"], row["kind"]) for row in rows(
        dsn, """SELECT chapter.revealed_at,chapter.kind FROM chapters AS chapter
                JOIN ingested_chapters AS receipt
                  ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                   =(chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                WHERE chapter.owner_id=%s AND chapter.book_id=%s
                ORDER BY chapter.revealed_at""", *scope)]


def built_for(dsn: str, owner: uuid.UUID, book) -> list[tuple[int, str]]:
    """(ordinal, kind) of every published chapter of the edition of a reader's book's file (ADR
    0044 step 3: a reader's runs build it, and the reader owns no story memory row)."""
    return built_in(dsn, edition_of(dsn, owner, book))


def edition_spend(dsn: str, scope: tuple) -> tuple[list[dict], list[dict]]:
    """The notes account's reservations and ledger for one edition (ADR 0044 step 4: story memory
    is paid by the notes account, against the edition being built)."""
    return tuple([row for row in found if row["book_id"] == scope[1]]
                 for found in spend_of(dsn, scope[0]))


def slot(chapter: int, scope: tuple, *, published: bool = True) -> dict:
    """A story-memory job's ``payload_metadata`` once it has named its chapter: the chapter and
    the edition it builds it in (the build slot, migration 0033), and, once its run published
    the chapter (was the first to build it), ``published`` (ADR 0044 step 4, which the chapter
    guards count)."""
    return {"chapter": chapter, "edition": str(scope[1]), **({"published": True} if published
                                                             else {})}


def expire_lease(dsn: str, owner: uuid.UUID) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE job_attempts SET leased_at=now()-interval '2 seconds',"
                     "lease_expires_at=now()-interval '1 second' "
                     "WHERE owner_id=%s AND finished_at IS NULL", (owner,))


def opened(memory_app, readers, label: str = "a", title: str = "The Lamp Book", *,
           reading: bool = True) -> tuple[Reader, str]:
    """A reader with the lamp book (``title`` makes another file). With ``reading``, they have
    read its first page, not skimmed (ADR 0044 section 3: story memory builds nothing for a book
    before that), which asks for the book's first story-memory run."""
    client, tokens, _app = memory_app
    someone = Reader(client, tokens, label, readers[label])
    book = upload(client, tokens, label, lamps(title))
    if reading:
        someone.read(book, 1, 1, 2)
    return someone, book


TOMORROW = ("SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day')"
            " AT TIME ZONE 'UTC' AS at")


# ---- 1: one waiting, one running, one chapter per run --------------------------------------------

def test_a_finished_chapter_makes_one_waiting_job_and_each_run_builds_one_chapter(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: an upload that asks for anything; a page or "I've read this" that
    asks for nothing (or for a job per page turn), a second job of a book waiting or running
    beside the first and paying twice, or a run that builds more than one chapter (holding up
    every other reader on the worker). ADR 0044 step 4: the first page of the book read (not
    skimmed) asks too, and the runs build one chapter past the last one finished."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, reading=False)
    assert rows(database, "SELECT id FROM jobs WHERE owner_id=%s", a.owner) == []
    a.read(book, 1, 1, 60)              # part 1 to its end, from after its heading: not whole
    [job] = jobs_of(database, a.owner)                    # the first page read asks
    assert (job["state"], str(job["book_id"]), job["payload_metadata"],
            job["priority"]) == ("pending", book, {}, 5)
    a.read(book, 2, 1, 30)              # half of part 2: asks for nothing
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM jobs WHERE owner_id=%s", (a.owner,))
    a.read(book, 2, 31, 40)
    assert jobs_of(database, a.owner) == []
    finishing = a.call("PUT", f"/api/books/{book}/read-page",
                       read_map_gate.page(1, 60, 60, to_chapter=2))
    assert finishing.status_code == 200, finishing.text   # part 2 read from its heading on
    [job] = jobs_of(database, a.owner)
    assert (job["state"], str(job["book_id"]), job["payload_metadata"],
            job["priority"]) == ("pending", book, {}, 5)
    queue = StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner))
    assert queue.ensure(book) is False
    a.mark(book, [2])                                     # asks again: nothing more waits
    assert len(jobs_of(database, a.owner)) == 1

    work = worker(app, extraction, worker_dsn)
    assert drain(work, 2) == 2                            # part 1, then the run it asked for
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    edition = edition_of(database, a.owner, book)
    assert no_memory_of_their_own(database, a.owner)      # the notes are the edition's
    assert [(row["state"], row["payload_metadata"], row["attempt_count"])
            for row in jobs_of(database, a.owner)] == [
        ("succeeded", slot(1, edition), 1), ("succeeded", slot(2, edition), 1),
        ("pending", {}, 0)]                               # part 3: one past part 2, finished
    assert models.parts() == [1, 2]

    # Part 3's run: while one job runs, one more may wait, and only one, unclaimed meanwhile.
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="first", lease_for=timedelta(seconds=60),
                                  kinds=frozenset({"story_memory"}))
    assert claim is not None and repository.start(claim)
    assert queue.ensure(book) is True and queue.ensure(book) is False
    assert [row["state"] for row in jobs_of(database, a.owner)] == [
        "succeeded", "succeeded", "running", "pending"]
    jobs_gate.step_clock_back(database, a.owner)       # the database host's clock stepped back
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, a.owner)] == [
        ("succeeded", slot(1, edition)), ("succeeded", slot(2, edition)), ("running", {}),
        ("pending", {})]
    assert repository.claim_next(worker_id="second", lease_for=timedelta(seconds=60)) is None
    dispatcher(app, extraction)(claim, repository)        # one run: part 3
    assert repository.succeed(claim)
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body"), (3, "body")]
    assert drain(work) == 1                               # the waiting one finds nothing to do
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, a.owner)] == [
        ("succeeded", slot(1, edition)), ("succeeded", slot(2, edition)),
        ("succeeded", slot(3, edition)), ("succeeded", {})]
    assert models.parts() == [1, 2, 3]                    # each part extracted once
    settled_once(database, notes_account(database))       # paid by the notes account
    assert spend_of(database, a.owner) == ([], [])


# ---- 2: the target is the read map ----------------------------------------------------------------

def test_the_target_is_the_read_map_never_a_counter_pushed_ahead(memory_app, readers, database,
                                                                  worker_dsn, monkeypatch):
    """The break it catches: extraction that follows the chapter counter (which a stale device's
    report can raise, open item 159) instead of the read map, building and paying for chapters
    more than one past what the reader has finished; queued work that goes on after "I haven't
    read this"; or memory already built thrown away when chapters are marked not read. ADR 0044
    step 4: the target is one chapter past the last one finished, so a reader who has read a
    page of part 1 and finished nothing has part 1 built for them (and only part 1)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    work = worker(app, extraction, worker_dsn)
    a, book = opened(memory_app, readers)                 # a page of part 1 read: target 1
    with psycopg.connect(database) as conn:
        # Both counters ahead: a device can raise only the first since ADR 0042 part 5, so this
        # stands for any counter ahead of the map.
        conn.execute("UPDATE reading_state SET bookmark=3,memory_through=3"
                     " WHERE owner_id=%s AND book_id=%s", (a.owner, book))
    catalog = HostedCompanionCatalog(database, OwnerId(a.owner))

    def unfinished_part_is_never_sent(schema, part):
        assert catalog.get_state(book)["bookmark"] == 3   # the counter is ahead meanwhile
        assert part == 1, part

    models = Lamps(extraction, monkeypatch, hook=unfinished_part_is_never_sent)
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body")]
    # Asked again with the counter still ahead, and with more of part 1 read: nothing more.
    a.read(book, 1, 3, 30)
    assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book) is True
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body")] and set(models.parts()) == {1}
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded", "succeeded"]

    # Parts 1 and 2 marked read (target 3); while part 2 is extracted the reader takes both
    # back. The chapter under way finishes; the work queued after it stops at the next chapter.
    models.hook = None
    a.mark(book, [1, 2])

    def taken_back(schema, part):
        if schema == "Extraction" and part == 2 and models.parts() == [1, 2]:
            a.mark(book, [1, 2], read=False)

    models.hook = taken_back
    assert drain(work) == 2
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert 3 not in models.parts()
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded"] * 4
    # What was built stays, even after part 1 is taken back too.
    before = edition_memory(database, a.owner, book)
    a.mark(book, [1], read=False)
    assert edition_memory(database, a.owner, book) == before
    assert len(before["ingested_chapters"]) == 2
    assert jobs_of(database, a.owner)[-1]["state"] == "succeeded"   # nothing new asked for


# ---- 3: money ---------------------------------------------------------------------------------------

def test_the_notes_ceiling_holds_every_new_chapter_whole_for_tomorrow_and_the_platform_bounds_all(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 step 4 moved this test from a capped reader to the notes ceiling: a reader's cap
    no longer holds story memory (``test_story_memory_one_ahead_postgres.py``). The break it
    catches: a chapter refused half-way by the ceiling once begun (its paid calls thrown away and
    paid again); a run at the ceiling that spends anything, is claimed and refused again and
    again, or does not wait for the next UTC day; extraction outside the notes account; or the
    platform's day not bounding story memory. B's file differs from A's (ADR 0044 step 3: one
    file's notes are built once, whoever reads it), so each reader's run is the first to its own
    file."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, usage=(500, 200))  # $0.0015 a call at terra
    account = notes_account(database)
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b", OTHER_FILE)
    update_limits(database, account, {"max_spend_usd_per_day": Decimal("0.001")})
    a.mark(book_a, [1])                                   # target 2
    work = worker(app, extraction, worker_dsn)
    # A's part 1 (admitted, finished whole past the ceiling); B's part 1 and A's part 2 refused
    # at their first call; each waits for tomorrow.
    assert drain(work) == 3
    assert built_for(database, a.owner, book_a) == [(1, "body")]
    assert built_for(database, b.owner, book_b) == []
    ledger = settled_once(database, account)
    assert {row["purpose"] for row in ledger} == {"extraction", "extraction-review"}
    assert sum(row["usd"] for row in ledger) > Decimal("0.001")   # finished past it, whole
    assert all(row["chapter_ordinal"] == 1 for row in ledger)
    assert spend_of(database, a.owner) == ([], []) == spend_of(database, b.owner)
    tomorrow = rows(database, TOMORROW)[0]["at"]
    for someone in (a, b):
        waiting = jobs_of(database, someone.owner)[-1]
        assert (waiting["state"], waiting["attempt_count"], waiting["run_after"]) == (
            "pending", 0, tomorrow)
    assert models.parts() == [1]                          # nothing of the others was asked for

    # The platform's day, used up, the ceiling lifted: B is refused at the first call, spends
    # nothing and waits for tomorrow.
    update_limits(database, account, {"max_spend_usd_per_day": Decimal("1")})
    calls, spent = list(models.calls), notes_spend(database)
    before = spend_limits()
    set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                     reader_usd_per_month=before.reader_usd_per_month,
                     platform_usd_per_day=Decimal("0.001"))
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE owner_id=%s AND state='pending'",
                     (b.owner,))
    assert work.run_once()
    held, job = jobs_of(database, b.owner)[-2:]
    assert (held["state"], job["state"], job["run_after"]) == ("succeeded", "pending", tomorrow)
    assert models.calls == calls and notes_spend(database) == spent
    assert built_for(database, b.owner, book_b) == []


# ---- 4: a chapter that cannot be done ---------------------------------------------------------------

@pytest.mark.parametrize("cause, code", [("reject", "grounding_rejected"),
                                         ("self_relation", "invalid_model_output"),
                                         ("malformed", "invalid_model_output")])
def test_a_chapter_failing_on_two_runs_is_kept_unremembered_and_the_book_goes_on(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch, cause, code):
    """The break it catches: a rejected chapter that stops the book's memory for good and is paid
    for again on every page turn (local mode's behaviour), or one given up after a single run; a
    chapter the hosted schema cannot hold (a person tied to themselves) failing the same way for
    ever, or published with part of what the review cleared silently dropped; or an unremembered
    chapter that asserts anything, or that the checks and screens read as holding something."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, **{cause: {2}})
    a, book = opened(memory_app, readers)
    a.mark(book, [1, 2, 3])
    assert drain(worker(app, extraction, worker_dsn)) == 5
    edition = edition_of(database, a.owner, book)
    failed = {"code": code, "retryable": False}
    # By chapter, failed runs first: two jobs asked for in quick succession can carry the same
    # (or, when the database host's clock steps, out of order) created_at.
    assert sorted(((row["state"], row["sanitized_error"], row["payload_metadata"],
                    row["attempt_count"]) for row in jobs_of(database, a.owner)),
                  key=lambda job: (job[2]["chapter"], job[0] != "failed")) == [
        ("succeeded", None, slot(1, edition), 1),
        ("failed", failed, slot(2, edition, published=False), 1),
        ("failed", failed, slot(2, edition, published=False), 1),
        ("succeeded", None, slot(2, edition), 1), ("succeeded", None, slot(3, edition), 1)]
    assert built_in(database, edition) == [(1, "body"), (2, "unremembered"), (3, "body")]
    # Two runs of part 2 (one extraction and its one repair or retry each, where there is one);
    # none after.
    assert models.parts() == ([1, 2, 2, 3] if cause == "self_relation" else [1, 2, 2, 2, 2, 3])
    settled_once(database, notes_account(database))

    # The unremembered chapter asserts nothing: no row is stamped with it, its summary is empty.
    with psycopg.connect(database) as conn:
        for table in ("entities", "aliases", "edges", "events", "event_participants", "themes",
                      "entity_state"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id=%s"
                                " AND revealed_at=2", edition).fetchone()[0] == 0, table
        assert conn.execute("SELECT summary FROM chapter_summaries WHERE owner_id=%s"
                            " AND book_id=%s AND revealed_at=2", edition).fetchall() == [("",)]
        assert conn.execute("SELECT count(*) FROM edges WHERE src_entity_id=dst_entity_id"
                            ).fetchone()[0] == 0

    # The unchanged readers of memory treat it as holding nothing: Corvus, first met in part 2,
    # is met where he is next named (part 3), so before that his name is a name not yet met.
    read_words = set(word_tokens(" ".join(line(n, i) for n in (1, 2, 3) for i in (1, 2))))
    read_words |= {"the", "lamplighter"}
    with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
        assert mem.completion_frontier(hosted_atoms(app, a.owner, book)) == 3
        assert [row["canonical_name"] for row in mem.view(2).characters()] == ["Mara"]
        assert sorted((row["canonical_name"], row["revealed_at"])
                      for row in mem.view(3).characters()) == [
            ("Corvus", 3), ("Mara", 1), ("Vex", 3)]
        assert same_person_names(mem, 2, read_words, {"vex", "lamplighter"}) == []
        assert same_person_names(mem, 3, read_words, {"lamplighter"}) == [
            ["Vex", "the Lamplighter"]]
        # Asked of the database (ADR 0044 step 10), which answers with the sentence's own word.
        for bookmark in (1, 2):
            assert score_recap(mem, bookmark, "Corvus moved lamp 50.")[
                "future_entity_leaks"] == ["corvus"]
        assert score_recap(mem, 3, "Corvus moved lamp 50.")["future_entity_leaks"] == []


# ---- 4b: never looser than local mode on the chat's spoiler probes ---------------------------------

PROBES = {
    "later": "Vex moved lamp 50 in part 3.",            # a person first met after the hole
    "in the hole": "Corvus moved lamp 50 in part 2.",   # a person first met in the hole
    "late alias": "The Lamplighter moved lamp 50.",     # another name, first given later
    "foreshadowing": "Mara will lose lamp 2 later.",
}


def fixed_check_drops(mem, durable: int, read_text: str) -> set[str]:
    """The probes the chat's fixed checks drop (``sentence_checks.safe`` in
    ``app/stream_conversation.py``): a name not yet met at ``durable`` and not in the read
    text, or foreshadowing. Jev and the second look are the same stand-ins in both modes."""
    words = set(word_tokens(read_text))
    dropped = set()
    for probe, sentence in PROBES.items():
        result = score_recap(mem, durable, sentence, read_text=read_text)
        if unread_future_names(result, words) or result["prolepsis_hits"]:
            dropped.add(probe)
    return dropped


def test_an_unremembered_chapter_is_never_looser_than_local_mode_on_the_spoiler_probes(
        memory_app, readers, database, tenant_dsn, worker_dsn, tmp_path, monkeypatch):
    """The break it catches: an unremembered chapter that lets through a sentence local mode
    would drop. Local mode in the same state (the review rejects part 2) stops the book's memory
    at part 1; hosted mode keeps part 2 empty and goes on to part 3. At places before, at and
    after the hole, every probe local mode's fixed checks drop (a person first met later, one
    first met in the hole, a late alias, foreshadowing) is dropped by hosted mode's, and the
    hosted chat, with a checker that clears everything, shows none of them."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch, reject={2})
    a, book = opened(memory_app, readers)
    a.mark(book, [1, 2, 3])
    assert drain(worker(app, extraction, worker_dsn)) == 5
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "unremembered"), (3, "body")]
    atoms = hosted_atoms(app, a.owner, book)
    # Each publish wrote the words of the names it added (ADR 0044 step 10), exactly as they are
    # made again from the edition's rows; no name is without them.
    words = ("SELECT source_kind, source_id, source_digest, tokens, folded, roles, words"
             " FROM name_words WHERE owner_id=%s AND book_id=%s ORDER BY 1, 2")
    with psycopg.connect(database) as conn:
        scope = conn.execute("SELECT owner_id, id, incarnation FROM books WHERE owner_id=%s"
                             " AND id=%s", edition_of(database, a.owner, book)[:2]).fetchone()
        published = conn.execute(words, scope[:2]).fetchall()
        assert {row[0] for row in published} == {"entity", "alias", "theme"}
        assert rebuild_name_words(conn, scope) == len(published)
        assert conn.execute(words, scope[:2]).fetchall() == published

    local_client = extraction_client()
    Lamps(local_client, monkeypatch, reject={2})
    with local_book(tmp_path, lamps()) as (extract, memory, local_atoms, chapters):
        extract(local_client, 1)
        for _again in range(2):                          # local mode stops here, every time
            with pytest.raises(ExtractionGroundingRejected):
                extract(local_client, 2)
        local, hosted = {}, {}
        for place in (1, 2, 3):
            read_text = " ".join(ch["text"] for ch in chapters if ch["ordinal"] <= place)
            with memory() as mem:
                frontier = mem.completion_frontier(local_atoms)
                local[place] = fixed_check_drops(mem, min(place, frontier), read_text)
            with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
                frontier = mem.completion_frontier(atoms)
                hosted[place] = fixed_check_drops(mem, min(place, frontier), read_text)
    for place in (1, 2, 3):
        assert local[place] <= hosted[place], place
    # A floor under the comparison: local mode drops only foreshadowing (its memory stops at
    # part 1); hosted mode also drops every name not yet met at that place.
    assert local == {1: {"foreshadowing"}, 2: {"foreshadowing"}, 3: {"foreshadowing"}}
    assert hosted == {1: set(PROBES), 2: {"later", "late alias", "foreshadowing"},
                      3: {"foreshadowing"}}

    # Through the hosted chat, the checker clearing every sentence: what is dropped is exactly
    # what the fixed checks drop, at each place. The notes are the edition's, handed through the
    # door only up to the reader's place (ADR 0044 step 3), and the later names are checked in
    # the database (step 10), so at each place, with the reader really there (``served``), the
    # chat drops what it dropped when the reader held every row: every name not yet met.
    platform = app.state.platform_client
    served = {}
    for place in (1, 2, 3):
        a.mark(book, list(range(1, place + 1)))
        if place < 3:
            a.mark(book, list(range(place + 1, 4)), read=False)
        read_text = " ".join(ch["text"] for ch in chapters if ch["ordinal"] <= place)
        with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
            assert mem.completion_frontier(atoms) == place
            served[place] = fixed_check_drops(mem, place, read_text)
        assert local[place] <= served[place] <= hosted[place], place
        for probe, sentence in PROBES.items():
            StandIns(platform, monkeypatch, reply=f"{line(1, 2)} {sentence}")
            shown = sentences(a.ask(book))
            assert line(1, 2) in shown, (place, probe)
            assert (sentence not in shown) == (probe in served[place]), (place, probe)
    assert served == hosted


# ---- 6: the writer refused, or unreachable ----------------------------------------------------------

def test_a_refused_writer_pauses_every_reader_and_an_outage_retries_neither_counted(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: the platform's refused writer account met by every reader's job in
    turn (each paying for what it reached), a pause per reader, an outage taken for a chapter
    that cannot be done (counted towards keeping it unremembered), or either stopping a book.
    B's file differs from A's, so B's run has a chapter of its own to build (ADR 0044 step 3)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    refused = ProviderUnavailable("authentication", service="completion")
    models = Lamps(extraction, monkeypatch, fail={("Extraction", 1): refused})
    a, book_a = opened(memory_app, readers, "a")          # a page of part 1 read: target 1
    b, book_b = opened(memory_app, readers, "b", OTHER_FILE)
    work = worker(app, extraction, worker_dsn)
    assert work.run_once()                                # A's job meets the refusal
    [job_a] = jobs_of(database, a.owner)
    assert (job_a["state"], job_a["sanitized_error"]) == (
        "pending", {"code": "provider_rejected", "retryable": True})
    pauses = rows(database, "SELECT provider,reason,paused_until-now() AS left FROM provider_pauses")
    assert [(p["provider"], p["reason"]) for p in pauses] == [("openai-compatible", "unauthorized")]
    assert timedelta(minutes=4) < pauses[0]["left"] <= timedelta(minutes=5)
    assert work.run_once() is False                       # every reader waits, B included
    [job_b] = jobs_of(database, b.owner)
    assert (job_b["state"], job_b["attempt_count"]) == ("pending", 0)
    assert built_for(database, a.owner, book_a) == [] == built_for(database, b.owner, book_b)
    assert models.parts() == [1]
    audit = rows(database, "SELECT owner_id FROM audit_events WHERE action='provider.pause'")
    assert [row["owner_id"] for row in audit] == [a.owner]

    with psycopg.connect(database) as conn:
        conn.execute("UPDATE provider_pauses SET paused_until=now()-interval '1 second'")
    assert drain(work) == 2
    assert built_for(database, a.owner, book_a) == [(1, "body")] \
        == built_for(database, b.owner, book_b)

    # A review call that cannot reach the writer (the review wraps the outage): retried, no pause.
    models.fail = {("ExtractionVerdict", 2): ProviderUnavailable("network", service="completion")}
    a.mark(book_a, [1])                                   # target 2
    assert work.run_once()
    job = jobs_of(database, a.owner)[-1]
    assert (job["state"], job["sanitized_error"]) == (
        "pending", {"code": "provider_unavailable", "retryable": True})
    assert rows(database, "SELECT count(*) AS n FROM provider_pauses WHERE paused_until>now()"
                )[0]["n"] == 0
    assert drain(work) == 1
    assert built_for(database, a.owner, book_a) == [(1, "body"), (2, "body")]
    assert not [row for row in jobs_of(database, a.owner) if row["state"] == "failed"]
    settled_once(database, notes_account(database))


def test_each_failure_of_a_run_becomes_what_the_adr_says() -> None:
    """The classification itself, for the failures the tests above do not reach with a real run."""
    from app.cost.limits import CostCeilingExceeded
    from app.hosted.companion.spend import ReaderAllowanceReached
    from app.hosted.companion.story_memory import WorkingCopyMismatch
    from app.ingest.extraction.validation import ExtractionSafetyError
    from app.llm.versioning import FORCE_RE_EXTRACT, ModelMigrationRequired

    def raised(exc, cause=None):
        try:
            raise exc from cause
        except Exception as caught:
            return story_memory_failure(caught)

    def failure(code, retryable):
        return (code, retryable)

    def as_pair(found):
        return None if found is None else (found.code, found.retryable)

    malformed = ValueError("malformed extraction after 2 attempts")
    capped = CostCeilingExceeded("day")
    cases = [
        (raised(ExtractionSafetyError("review invalid"), ValueError("bad verdict")),
         failure("invalid_model_output", False)),
        (raised(ExtractionSafetyError("too many batches")), failure("invalid_model_output", False)),
        (raised(malformed, TypeError("not a dict")), failure("invalid_model_output", False)),
        (raised(CostCeilingExceeded("completion input exceeds per-call ceiling")),
         failure("invalid_model_output", False)),
        (raised(capped, ReaderAllowanceReached("day", "resting")), None),
        (raised(ExtractionSafetyError("review unavailable"), psycopg.OperationalError("down")),
         None),
        (raised(ExtractionSafetyError("review unavailable"),
                ProviderUnavailable("rate_limit", service="completion")),
         failure("provider_unavailable", True)),
        (raised(WorkingCopyMismatch("stale")), failure("source_integrity", False)),
        (raised(ModelMigrationRequired([("extractor_model", FORCE_RE_EXTRACT)])),
         failure("internal_error", False)),
        (raised(ValueError("the book has no such chapter")), None),
        (raised(RuntimeError("anything else")), None),
    ]
    assert [as_pair(found) for found, _expected in cases] == [expected for _found, expected in cases]


# ---- 5: a lost lease, a removed book, a dead worker -------------------------------------------------

def test_a_lease_lost_mid_chapter_is_published_once_and_settled_once(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: a worker whose lease ran out mid-chapter (another worker has since
    taken the job and published the chapter) publishing it again, publishing half, or leaving
    its calls charged twice or not at all."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    a, book = opened(memory_app, readers)                 # a page of part 1 read: target 1
    first = worker(app, extraction, worker_dsn, "first")
    second = worker(app, extraction, worker_dsn, "second")
    after = {}

    def another_worker_takes_over(schema, part):
        if after:
            return
        after["claimed while live"] = second.run_once()
        expire_lease(database, a.owner)
        after["ran"] = second.run_once()
        after["built"] = built_for(database, a.owner, book)

    Lamps(extraction, monkeypatch, hook=another_worker_takes_over)
    assert first.run_once()
    assert after == {"claimed while live": False, "ran": True, "built": [(1, "body")]}
    assert built_for(database, a.owner, book) == [(1, "body")]
    assert len(edition_memory(database, a.owner, book)["ingested_chapters"]) == 1
    ledger = settled_once(database, notes_account(database))
    assert "extraction-abandoned" in {row["purpose"] for row in ledger}
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("succeeded", 2)
    attempts = rows(database, "SELECT worker_id,outcome FROM job_attempts WHERE owner_id=%s"
                              " ORDER BY attempt_no", a.owner)
    assert [(row["worker_id"], row["outcome"]) for row in attempts] == [
        ("first", "expired"), ("second", "succeeded")]


def test_removing_the_book_mid_chapter_cancels_the_job_and_leaves_nothing(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: a chapter published, or paid on, for a book the reader removed while
    it was being extracted, or the job left running or retried."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    a, book = opened(memory_app, readers)
    a.mark(book, [1, 2])
    edition = {}

    def remove_the_book(schema, part):
        if models.parts() == [1]:
            # The edition the run builds into (made on its first chapter), noted before the
            # removal clears the book's fingerprints.
            edition["scope"] = edition_of(database, a.owner, book)
            assert a.call("DELETE", f"/api/books/{book}").status_code == 204

    models = Lamps(extraction, monkeypatch, hook=remove_the_book)
    assert worker(app, extraction, worker_dsn).run_once()
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["cancelled"]
    assert all(found == [] for found in book_memory(database, edition["scope"]).values())
    assert no_memory_of_their_own(database, a.owner)
    assert models.calls == [("Extraction", 1)]            # nothing after the removal
    settled_once(database, notes_account(database))       # the call made is charged once
    assert drain(worker(app, extraction, worker_dsn)) == 0


def test_a_worker_that_died_mid_chapter_is_resumed_from_rows_by_a_new_one(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: work kept in a worker's memory and lost with it, a chapter left
    refused for good because the dead run's calls stayed reserved, or those calls charged twice."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    a, book = opened(memory_app, readers)                 # a page of part 1 read: target 1

    def dies(schema, part):
        raise KeyboardInterrupt("the worker died mid-chapter")

    models = Lamps(extraction, monkeypatch, hook=dies)
    with pytest.raises(KeyboardInterrupt):
        worker(app, extraction, worker_dsn, "gone").run_once()
    assert any(row["state"] == "reserved" for row in notes_spend(database)[0])
    expire_lease(database, a.owner)

    models.hook = None
    restarted = worker(app, extraction_client(), worker_dsn, "restarted")
    Lamps(restarted.handler._extraction_client, monkeypatch)
    assert restarted.run_once()
    assert built_for(database, a.owner, book) == [(1, "body")]
    ledger = settled_once(database, notes_account(database))
    assert "extraction-abandoned" in {row["purpose"] for row in ledger}
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("succeeded", 2)


# ---- 7: two readers -------------------------------------------------------------------------------

def test_two_readers_story_memory_jobs_memory_and_spend_never_touch(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The break it catches: one reader's job reading or writing the other's rows or the notes of
    the other's file (the files differ by their title alone, so their chapters are the same and
    only the fingerprints tell them apart), the rejections of one file's chapter counted
    towards another file's, a second reader learning of or adding (and paying for) a job on the
    first reader's book, through the routes, the row-security role alone, or the queue's own
    predicates alone. ADR 0044 step 3: two readers of the very same file share its edition, and
    its failures, by design (``test_story_memory_editions_postgres.py``)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, reject={2})
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b", OTHER_FILE, reading=False)
    a.mark(book_a, [1, 2, 3])
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 5
    a_edition = edition_of(database, a.owner, book_a)
    a_rows, a_jobs, a_spend = (book_memory(database, a_edition), jobs_of(database, a.owner),
                               edition_spend(database, a_edition))
    assert built_in(database, a_edition) == [(1, "body"), (2, "unremembered"), (3, "body")]

    # B's part 2 passes its review: the two rejections of A's file are that file's alone.
    models.reject = set()
    b.read(book_b, 1, 1, 2)
    b.mark(book_b, [1, 2, 3])
    assert drain(work) == 3
    b_edition = edition_of(database, b.owner, book_b)
    assert b_edition != a_edition
    assert built_in(database, b_edition) == [(1, "body"), (2, "body"), (3, "body")]
    assert (book_memory(database, a_edition), jobs_of(database, a.owner),
            edition_spend(database, a_edition)) == (a_rows, a_jobs, a_spend)
    assert no_memory_of_their_own(database, a.owner)
    assert no_memory_of_their_own(database, b.owner)
    with psycopg.connect(database) as conn:
        for table in ("jobs", "cost_reservations", "cost_ledger"):
            mixed = conn.execute(
                f"SELECT count(*) FROM {table} WHERE (owner_id=%s AND book_id<>%s)"
                " OR (owner_id=%s AND book_id<>%s)",
                (a.owner, uuid.UUID(book_a), b.owner, uuid.UUID(book_b))).fetchone()[0]
            assert mixed == 0, table
        # Story memory's spend (ADR 0044 step 4) is the notes account's, on the two editions.
        for table in ("cost_reservations", "cost_ledger"):
            mixed = conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id<>ALL(%s)",
                (a_edition[0], [a_edition[1], b_edition[1]])).fetchone()[0]
            assert mixed == 0, table
        for table in ("chapters", "entities", "ingested_chapters", "entity_state", "events"):
            mixed = conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id=%s AND book_id<>ALL(%s)",
                (a_edition[0], [a_edition[1], b_edition[1]])).fetchone()[0]
            assert mixed == 0, table
        # B's jobs name B's edition only, A's jobs A's.
        named = conn.execute("SELECT DISTINCT owner_id,payload_metadata->>'edition' FROM jobs"
                             " WHERE kind='story_memory' AND payload_metadata ? 'edition'"
                             ).fetchall()
    assert sorted(named) == sorted([(a.owner, str(a_edition[1])), (b.owner, str(b_edition[1]))])

    # B's routes answer A's book as missing, and add nothing for it.
    missing = uuid.uuid4()
    for suffix, body in (("/read-chapters", {"atoms": [1, 2, 3], "read": True}),
                         ("/read-page", read_map_gate.page(1, 1, 60))):
        hidden = b.call("PUT", f"/api/books/{book_a}{suffix}", body)
        absent = b.call("PUT", f"/api/books/{missing}{suffix}", body)
        assert (hidden.status_code, hidden.json()) == (absent.status_code, absent.json())
        assert hidden.status_code == 404
    # The queue bound to B adds nothing on A's book: with row-level security, and on a
    # superuser connection with its own predicates alone.
    for dsn in (tenant_dsn, database):
        assert StoryMemoryQueue(dsn, OwnerId(b.owner)).ensure(book_a) is False
    assert jobs_of(database, a.owner) == a_jobs
    incarnation = rows(database, "SELECT incarnation FROM books WHERE id=%s", book_a)[0][
        "incarnation"]
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b.owner),))
        assert conn.execute("SELECT count(*) FROM jobs WHERE kind='story_memory'"
                            " AND owner_id<>%s", (b.owner,)).fetchone()[0] == 0
        assert conn.execute("UPDATE jobs SET priority=99 WHERE kind='story_memory'"
                            " AND owner_id=%s", (a.owner,)).rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("""INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,
                                              idempotency_key,priority)
                            VALUES (%s,%s,%s,'story_memory','story-memory:planted',5)""",
                         (a.owner, book_a, incarnation))
    assert (book_memory(database, a_edition), jobs_of(database, a.owner)) == (a_rows, a_jobs)


# ---- 8: erasure, removal and a copy, with these jobs present ---------------------------------------

def test_a_copy_removal_and_erasure_still_work_with_story_memory_jobs_and_rows(
        memory_app, readers, database, admin_dsn, worker_dsn, monkeypatch):
    """The break it catches: story-memory jobs (finished, failed on a chapter, waiting) or their
    rows that stop a copy of one's data, block removing the book, or survive erasing the
    account; or a copy that hands out story memory rows (ADR 0042 section 7: only the view at
    the reader's place, which is not built yet). ADR 0044 step 3: the notes the jobs built are
    the edition's, which removal and erasure leave as they are (section 6)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch, reject={2})
    a, book = opened(memory_app, readers)
    a.mark(book, [1])                                     # target 2 (ADR 0044 step 4)
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 4                               # 1, 2 rejected twice, 2 unremembered
    edition = edition_of(database, a.owner, book)
    notes = book_memory(database, edition)
    assert built_in(database, edition) == [(1, "body"), (2, "unremembered")]
    assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book)   # waiting
    second = upload(a.client, a.tokens, "a", lamps())
    a.mark(second, [1])                                   # one more, left waiting
    assert {row["state"] for row in jobs_of(database, a.owner)} == {
        "succeeded", "failed", "pending"}

    files = copy_of(a.call("GET", "/api/account/export"))
    everything = "\n".join(files.values())
    assert "Lamplighter" not in everything and "Corvus moved the lamps" not in everything

    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    assert not built(database, a.owner) and no_memory_of_their_own(database, a.owner)
    assert book_memory(database, edition) == notes
    assert [(str(row["book_id"]), row["state"]) for row in jobs_of(database, a.owner)
            if row["state"] in ("pending", "cancelled")] == [(book, "cancelled"),
                                                             (second, "pending")]

    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
        conn.execute("UPDATE users SET status='deleting',erase_after=now()-interval '1 day'"
                     " WHERE id=%s", (a.owner,))
    with _role(admin_dsn, database, bypass=True, tables=tables) as purge_dsn:
        assert purge_account(PostgresAccountPurge(purge_dsn), app.state.object_storage, a.owner)
    assert jobs_of(database, a.owner) == [] and all(
        found == [] for found in memory_of(database, a.owner).values())
    assert book_memory(database, edition) == notes


# ---- 9: switched off ------------------------------------------------------------------------------

def test_with_story_memory_off_nothing_is_made_or_spent(
        tenant_dsn, readers, database, worker_dsn, tmp_path, monkeypatch):
    """The break it catches: a job made, or money spent, for a reader's book while the owner has
    not turned story memory on (HOSTED_STORY_MEMORY), or the kind reachable at all then. ADR 0044
    step 4 removed the setting for a reader's own books, so its case is gone; what it guarded
    (nothing built or spent for a book nobody reads) is
    ``test_story_memory_one_ahead_postgres.py::test_an_upload_a_skimmed_page_and_chapters_marked_read_without_a_read_page_build_and_spend_nothing``."""
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **CHAT)
    with client:
        extraction = extraction_client()
        models = Lamps(extraction, monkeypatch)
        a = Reader(client, tokens, "a", readers["a"])
        book = upload(client, tokens, "a", lamps())
        a.read(book, 1, 1, 60)
        a.mark(book, [2, 3])
        assert jobs_of(database, a.owner) == []
        # A job left from before (or planted): no worker of this deployment takes it.
        assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book)
        notes = CompanionJobs(tenant_dsn=app.state.companion_dsn, settings=app.state.settings,
                              client=app.state.platform_client,
                              storage=app.state.object_storage,
                              sources=app.state.companion_sources,
                              kinds=frozenset({"chapter_notes", "recollect"}))
        work = DurableWorker(PostgresWorkerRepository(worker_dsn), notes, "companion",
                             lease_for=timedelta(seconds=60))
        assert work.run_once() is False
        assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"]
        assert models.calls == [] and spend_of(database, a.owner) == ([], [])
        assert notes_spend(database) == ([], [])
        assert built(database, a.owner) == [] == built_for(database, a.owner, book)


def test_story_memory_jobs_are_well_formed_or_refused_by_the_schema(memory_app, readers,
                                                                    database):
    """The break it catches: a story-memory job naming no book (none can name a credential: the
    column went in migration 0029), a second one waiting for the same book inserted around the queue, or a failure code outside
    the reviewed vocabulary."""
    a, book = opened(memory_app, readers)
    a.mark(book, [1])
    incarnation = rows(database, "SELECT incarnation FROM books WHERE id=%s", book)[0][
        "incarnation"]
    with psycopg.connect(database, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO jobs (owner_id,kind,idempotency_key) "
                         "VALUES (%s,'story_memory','story-memory:no-book')", (a.owner,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,"
                         "idempotency_key) VALUES (%s,%s,%s,'story_memory','story-memory:twin')",
                         (a.owner, book, incarnation))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE jobs SET sanitized_error='{\"code\":\"chapter_lost\","
                         "\"retryable\":false}'::jsonb WHERE owner_id=%s", (a.owner,))
        conn.execute("UPDATE jobs SET sanitized_error='{\"code\":\"grounding_rejected\","
                     "\"retryable\":false}'::jsonb WHERE owner_id=%s", (a.owner,))
    assert len(jobs_of(database, a.owner)) == 1


def test_with_story_memory_on_or_off_an_upload_makes_no_job(
        tenant_dsn, readers, database, tmp_path):
    """ADR 0042 section 6 and ADR 0043 step 7: the reader-key extraction job, which never had a
    handler, is gone; an upload makes no job of any kind, with story memory on or off (its jobs
    follow the reader's reading)."""
    for label, values in (("a", MEMORY), ("b", CHAT)):
        client, tokens, _app = memory_app_for(tenant_dsn, readers, tmp_path / label, **values)
        with client:
            upload(client, tokens, label, lamps())
        assert rows(database, "SELECT id FROM jobs WHERE owner_id=%s", readers[label]) == []


# ---- the screens show what the jobs build (parts 3 and 4 together) -----------------------------

def test_the_story_screens_show_what_the_jobs_built_and_only_up_to_the_readers_place(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: the screens of part 4 working on hand-written rows but not on the
    rows the jobs really publish; a person of a chapter the reader has not finished on the cast
    (ADR 0044 step 4: part 3 is built one ahead of the reader, and must stay out of sight); a
    cast that stays when a chapter is marked not read; or one reader seeing another's."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, reading=False)

    def screen(reader, path):
        return reader.call("GET", f"/api/books/{book}/{path}")

    # Nothing built and nothing asked for yet: the empty cast every book shows before its first
    # finished chapter (ADR 0044 step 5; it was a 404 before).
    assert (screen(a, "graph").status_code, screen(a, "graph").json()) == (
        200, {"as_of_chapter": 0, "characters": [], "relationships": []})

    a.read(book, 1, 1, 2)
    a.mark(book, [1, 2])                                  # parts 1 and 2 read; part 3 is not
    assert drain(worker(app, extraction, worker_dsn)) == 3
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body"), (3, "body")]

    graph = screen(a, "graph").json()
    assert graph["as_of_chapter"] == 2
    assert sorted(person["canonical_name"] for person in graph["characters"]) == ["Corvus", "Mara"]
    notes = screen(a, "notes").json()
    assert sorted((chapter["revealed_at"], chapter["summary"]) for chapter in notes["chapters"]) == [
        (1, "Mara moved the lamps."), (2, "Corvus moved the lamps.")]
    progress = screen(a, "ingest").json()
    assert (progress["ingest_progress"], progress["status"]) == (2, "idle")
    # Part 3's people are nowhere: built one ahead, never handed to this reader, and asking for
    # a later chapter changes nothing.
    assert "Vex" not in screen(a, "graph?bookmark=99").text
    assert "Lamplighter" not in screen(a, "notes?bookmark=99").text
    corvus = next(p for p in graph["characters"] if p["canonical_name"] == "Corvus")
    assert screen(a, f"character/{corvus['entity_id']}").status_code == 200

    # "I haven't read part 2": the built memory stays in the database, the screens show less.
    a.mark(book, [2], read=False)
    shrunk = screen(a, "graph").json()
    assert [person["canonical_name"] for person in shrunk["characters"]] == ["Mara"]
    assert screen(a, f"character/{corvus['entity_id']}").status_code == 404
    assert "Corvus" not in screen(a, "notes").text
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body"), (3, "body")]

    # The second reader has nothing of it, by any route.
    b = Reader(_client, _tokens, "b", readers["b"])
    for path in ("graph", "notes", "ingest", f"character/{corvus['entity_id']}"):
        assert screen(b, path).status_code == 404
