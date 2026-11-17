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

from app.eval.spoiler_gate import score_recap
from app.hosted.companion.jobs import CompanionJobs, StoryMemoryQueue, story_memory_failure
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
from tests.hosted.test_story_memory_postgres import memory_of, settled_once, spend_of
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

MEMORY = {**CHAT, "hosted_story_memory": True, "hosted_reader_story_memory": True}
GREETING = "Corvus greeted Vex at the quay."


def lamps() -> bytes:
    """The chat tests' lamp book (Mara in part 1, Corvus in part 2, Vex in part 3, sixty lines
    each), with Corvus named once more at the end of part 3."""
    parts = [" ".join(line(n, i) for i in range(1, 61)) for n in (1, 2, 3)]
    parts[2] += " " + GREETING
    return epub_ncx([(f"c{n}.xhtml", f"Part {n}", f"Part {n}", text)
                     for n, text in enumerate(parts, start=1)],
                    title="The Lamp Book", author="Ada Writer")


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
    return rows(dsn, "SELECT * FROM jobs WHERE owner_id=%s AND kind=%s ORDER BY created_at,id",
                owner, kind)


def built(dsn: str, owner: uuid.UUID) -> list[tuple[int, str]]:
    """(ordinal, kind) of every chapter of the owner whose story memory is published."""
    return [(row["revealed_at"], row["kind"]) for row in rows(
        dsn, """SELECT chapter.revealed_at,chapter.kind FROM chapters AS chapter
                JOIN ingested_chapters AS receipt
                  ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
                   =(chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
                WHERE chapter.owner_id=%s ORDER BY chapter.revealed_at""", owner)]


def expire_lease(dsn: str, owner: uuid.UUID) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE job_attempts SET leased_at=now()-interval '2 seconds',"
                     "lease_expires_at=now()-interval '1 second' "
                     "WHERE owner_id=%s AND finished_at IS NULL", (owner,))


def opened(memory_app, readers, label: str = "a") -> tuple[Reader, str]:
    client, tokens, _app = memory_app
    someone = Reader(client, tokens, label, readers[label])
    return someone, upload(client, tokens, label, lamps())


TOMORROW = ("SELECT (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day')"
            " AT TIME ZONE 'UTC' AS at")


# ---- 1: one waiting, one running, one chapter per run --------------------------------------------

def test_a_finished_chapter_makes_one_waiting_job_and_each_run_builds_one_chapter(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: a page or "I've read this" that asks for nothing (or for a job per
    page turn), a second job of a book waiting or running beside the first and paying twice, or
    a run that builds more than one chapter (holding up every other reader on the worker)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers)
    assert jobs_of(database, a.owner) == [] and jobs_of(database, a.owner, "ingest_book") == []
    a.read(book, 1, 1, 60)              # part 1 to its end, from after its heading: not whole
    a.read(book, 2, 1, 30)              # half of part 2
    assert jobs_of(database, a.owner) == []
    finishing = a.call("PUT", f"/api/books/{book}/read-page",
                       read_map_gate.page(1, 60, 60, to_chapter=2))
    assert finishing.status_code == 200, finishing.text   # part 2 read from its heading on
    [job] = jobs_of(database, a.owner)
    assert (job["state"], str(job["book_id"]), job["credential_id"], job["payload_metadata"],
            job["priority"]) == ("pending", book, None, {}, 5)
    queue = StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner))
    assert queue.ensure(book) is False
    a.mark(book, [2])                                     # asks again: nothing more waits
    assert len(jobs_of(database, a.owner)) == 1

    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 2                               # part 1, then the run it asked for
    assert built(database, a.owner) == [(1, "body"), (2, "body")]
    assert [(row["state"], row["payload_metadata"], row["attempt_count"])
            for row in jobs_of(database, a.owner)] == [
        ("succeeded", {"chapter": 1}, 1), ("succeeded", {"chapter": 2}, 1)]
    assert models.parts() == [1, 2]

    # Part 3 read: while one job runs, one more may wait, and only one, unclaimed meanwhile.
    a.mark(book, [3])
    repository = PostgresWorkerRepository(worker_dsn)
    claim = repository.claim_next(worker_id="first", lease_for=timedelta(seconds=60),
                                  kinds=frozenset({"story_memory"}))
    assert claim is not None and repository.start(claim)
    assert queue.ensure(book) is True and queue.ensure(book) is False
    assert [row["state"] for row in jobs_of(database, a.owner)] == [
        "succeeded", "succeeded", "running", "pending"]
    assert repository.claim_next(worker_id="second", lease_for=timedelta(seconds=60)) is None
    dispatcher(app, extraction)(claim, repository)        # one run: part 3
    assert repository.succeed(claim)
    assert built(database, a.owner) == [(1, "body"), (2, "body"), (3, "body")]
    assert drain(work) == 1                               # the waiting one finds nothing to do
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, a.owner)] == [
        ("succeeded", {"chapter": 1}), ("succeeded", {"chapter": 2}),
        ("succeeded", {"chapter": 3}), ("succeeded", {})]
    assert models.parts() == [1, 2, 3]                    # each part extracted once
    settled_once(database, a.owner)


# ---- 2: the target is the read map ----------------------------------------------------------------

def test_the_target_is_the_read_map_never_a_counter_pushed_ahead(memory_app, readers, database,
                                                                  worker_dsn, monkeypatch):
    """The break it catches: extraction that follows the chapter counter (which a stale device's
    report can raise, open item 159) instead of the read map, building and paying for chapters
    the reader has not finished; queued work that goes on after "I haven't read this"; or memory
    already built thrown away when chapters are marked not read."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    work = worker(app, extraction, worker_dsn)
    a, book = opened(memory_app, readers)
    a.mark(book, [1])
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reading_state SET bookmark=3 WHERE owner_id=%s AND book_id=%s",
                     (a.owner, book))
    catalog = HostedCompanionCatalog(database, OwnerId(a.owner))

    def unfinished_part_is_never_sent(schema, part):
        assert catalog.get_state(book)["bookmark"] == 3   # the counter is ahead meanwhile
        assert part == 1, part

    models = Lamps(extraction, monkeypatch, hook=unfinished_part_is_never_sent)
    assert drain(work) == 1
    assert built(database, a.owner) == [(1, "body")]
    # Asked again with the counter still ahead, and with part 2 half read: nothing more.
    a.read(book, 2, 1, 30)
    assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book) is True
    assert drain(work) == 1
    assert built(database, a.owner) == [(1, "body")] and set(models.parts()) == {1}
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded", "succeeded"]

    # Parts 2 and 3 marked read; while part 2 is extracted the reader takes both back. The
    # chapter under way finishes; the work queued after it stops at the next chapter.
    models.hook = None
    a.mark(book, [2, 3])

    def taken_back(schema, part):
        if schema == "Extraction" and part == 2 and models.parts() == [1, 2]:
            a.mark(book, [2, 3], read=False)

    models.hook = taken_back
    assert drain(work) == 2
    assert built(database, a.owner) == [(1, "body"), (2, "body")]
    assert 3 not in models.parts()
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded"] * 4
    # What was built stays, even after part 1 is taken back too.
    before = memory_of(database, a.owner)
    a.mark(book, [1], read=False)
    assert memory_of(database, a.owner) == before and len(before["ingested_chapters"]) == 2
    assert jobs_of(database, a.owner)[-1]["state"] == "succeeded"   # nothing new asked for


# ---- 3: money ---------------------------------------------------------------------------------------

def test_a_capped_reader_waits_whole_for_tomorrow_others_go_on_and_the_platform_bounds_all(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: a chapter refused half-way by the day cap once begun (its paid calls
    thrown away and paid again); a capped reader's job claimed and refused again and again, or
    holding up another reader's; extraction outside the reader's allowance; or the platform's day
    not bounding everyone."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, usage=(500, 200))  # $0.0015 a call at terra
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    update_limits(database, a.owner, {"max_spend_usd_per_day": Decimal("0.001")})
    a.mark(book_a, [1, 2])
    b.mark(book_b, [1])
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 2                               # A's part 1, B's part 1; A's next waits
    assert built(database, a.owner) == [(1, "body")] == built(database, b.owner)
    ledger = settled_once(database, a.owner)
    assert {row["purpose"] for row in ledger} == {"extraction", "extraction-review"}
    assert sum(row["usd"] for row in ledger) > Decimal("0.001")   # finished past the cap, whole
    assert all(row["chapter_ordinal"] == 1 for row in ledger)
    waiting = jobs_of(database, a.owner)[-1]
    tomorrow = rows(database, TOMORROW)[0]["at"]
    assert (waiting["state"], waiting["attempt_count"], waiting["run_after"]) == (
        "pending", 0, tomorrow)
    assert models.parts() == [1, 1]                       # nothing of A's part 2 was asked for

    # The platform's day, used up: B (under their own cap) is refused at the first call.
    calls, spent = list(models.calls), spend_of(database, b.owner)
    before = spend_limits()
    set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                     reader_usd_per_month=before.reader_usd_per_month,
                     platform_usd_per_day=Decimal("0.001"))
    b.mark(book_b, [2])
    assert work.run_once()
    job = jobs_of(database, b.owner)[-1]
    assert (job["state"], job["sanitized_error"]) == (
        "pending", {"code": "budget_exceeded", "retryable": True})
    assert models.calls == calls and spend_of(database, b.owner) == spent
    assert built(database, b.owner) == [(1, "body")]


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
    failed = {"code": code, "retryable": False}
    # By chapter, failed runs first: two jobs asked for in quick succession can carry the same
    # (or, when the database host's clock steps, out of order) created_at.
    assert sorted(((row["state"], row["sanitized_error"], row["payload_metadata"],
                    row["attempt_count"]) for row in jobs_of(database, a.owner)),
                  key=lambda job: (job[2]["chapter"], job[0] != "failed")) == [
        ("succeeded", None, {"chapter": 1}, 1), ("failed", failed, {"chapter": 2}, 1),
        ("failed", failed, {"chapter": 2}, 1), ("succeeded", None, {"chapter": 2}, 1),
        ("succeeded", None, {"chapter": 3}, 1)]
    assert built(database, a.owner) == [(1, "body"), (2, "unremembered"), (3, "body")]
    # Two runs of part 2 (one extraction and its one repair or retry each, where there is one);
    # none after.
    assert models.parts() == ([1, 2, 2, 3] if cause == "self_relation" else [1, 2, 2, 2, 2, 3])
    settled_once(database, a.owner)

    # The unremembered chapter asserts nothing: no row is stamped with it, its summary is empty.
    with psycopg.connect(database) as conn:
        for table in ("entities", "aliases", "edges", "events", "event_participants", "themes",
                      "entity_state"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE owner_id=%s"
                                " AND revealed_at=2", (a.owner,)).fetchone()[0] == 0, table
        assert conn.execute("SELECT summary FROM chapter_summaries WHERE owner_id=%s"
                            " AND revealed_at=2", (a.owner,)).fetchall() == [("",)]
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
        for bookmark in (1, 2):
            assert score_recap(mem, bookmark, "Corvus moved lamp 50.")[
                "future_entity_leaks"] == ["Corvus"]
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
        leaks = [name for name in result["future_entity_leaks"]
                 if not set(word_tokens(name)) <= words]
        if leaks or result["prolepsis_hits"]:
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
    assert built(database, a.owner) == [(1, "body"), (2, "unremembered"), (3, "body")]
    atoms = hosted_atoms(app, a.owner, book)

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
    # what the fixed checks drop, at each place.
    platform = app.state.platform_client
    for place in (1, 2, 3):
        a.mark(book, list(range(1, place + 1)))
        if place < 3:
            a.mark(book, list(range(place + 1, 4)), read=False)
        for probe, sentence in PROBES.items():
            StandIns(platform, monkeypatch, reply=f"{line(1, 2)} {sentence}")
            shown = sentences(a.ask(book))
            assert line(1, 2) in shown, (place, probe)
            assert (sentence not in shown) == (probe in hosted[place]), (place, probe)


# ---- 6: the writer refused, or unreachable ----------------------------------------------------------

def test_a_refused_writer_pauses_every_reader_and_an_outage_retries_neither_counted(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: the platform's refused writer account met by every reader's job in
    turn (each paying for what it reached), a pause per reader, an outage taken for a chapter
    that cannot be done (counted towards keeping it unremembered), or either stopping a book."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    refused = ProviderUnavailable("authentication", service="completion")
    models = Lamps(extraction, monkeypatch, fail={("Extraction", 1): refused})
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    a.mark(book_a, [1])
    b.mark(book_b, [1])
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
    assert built(database, a.owner) == [] == built(database, b.owner)
    assert models.parts() == [1]
    audit = rows(database, "SELECT owner_id FROM audit_events WHERE action='provider.pause'")
    assert [row["owner_id"] for row in audit] == [a.owner]

    with psycopg.connect(database) as conn:
        conn.execute("UPDATE provider_pauses SET paused_until=now()-interval '1 second'")
    assert drain(work) == 2
    assert built(database, a.owner) == [(1, "body")] == built(database, b.owner)

    # A review call that cannot reach the writer (the review wraps the outage): retried, no pause.
    models.fail = {("ExtractionVerdict", 2): ProviderUnavailable("network", service="completion")}
    a.mark(book_a, [2])
    assert work.run_once()
    job = jobs_of(database, a.owner)[-1]
    assert (job["state"], job["sanitized_error"]) == (
        "pending", {"code": "provider_unavailable", "retryable": True})
    assert rows(database, "SELECT count(*) AS n FROM provider_pauses WHERE paused_until>now()"
                )[0]["n"] == 0
    assert drain(work) == 1
    assert built(database, a.owner) == [(1, "body"), (2, "body")]
    assert not [row for row in jobs_of(database, a.owner) if row["state"] == "failed"]
    settled_once(database, a.owner)


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
    a, book = opened(memory_app, readers)
    a.mark(book, [1])
    first = worker(app, extraction, worker_dsn, "first")
    second = worker(app, extraction, worker_dsn, "second")
    after = {}

    def another_worker_takes_over(schema, part):
        if after:
            return
        after["claimed while live"] = second.run_once()
        expire_lease(database, a.owner)
        after["ran"] = second.run_once()
        after["built"] = built(database, a.owner)

    Lamps(extraction, monkeypatch, hook=another_worker_takes_over)
    assert first.run_once()
    assert after == {"claimed while live": False, "ran": True, "built": [(1, "body")]}
    assert built(database, a.owner) == [(1, "body")]
    assert len(memory_of(database, a.owner)["ingested_chapters"]) == 1
    ledger = settled_once(database, a.owner)
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

    def remove_the_book(schema, part):
        if models.parts() == [1]:
            assert a.call("DELETE", f"/api/books/{book}").status_code == 204

    models = Lamps(extraction, monkeypatch, hook=remove_the_book)
    assert worker(app, extraction, worker_dsn).run_once()
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["cancelled"]
    assert all(found == [] for found in memory_of(database, a.owner).values())
    assert models.calls == [("Extraction", 1)]            # nothing after the removal
    settled_once(database, a.owner)                       # the call made is charged once
    assert drain(worker(app, extraction, worker_dsn)) == 0


def test_a_worker_that_died_mid_chapter_is_resumed_from_rows_by_a_new_one(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: work kept in a worker's memory and lost with it, a chapter left
    refused for good because the dead run's calls stayed reserved, or those calls charged twice."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    a, book = opened(memory_app, readers)
    a.mark(book, [1])

    def dies(schema, part):
        raise KeyboardInterrupt("the worker died mid-chapter")

    models = Lamps(extraction, monkeypatch, hook=dies)
    with pytest.raises(KeyboardInterrupt):
        worker(app, extraction, worker_dsn, "gone").run_once()
    assert any(row["state"] == "reserved" for row in spend_of(database, a.owner)[0])
    expire_lease(database, a.owner)

    models.hook = None
    restarted = worker(app, extraction_client(), worker_dsn, "restarted")
    Lamps(restarted.handler._extraction_client, monkeypatch)
    assert restarted.run_once()
    assert built(database, a.owner) == [(1, "body")]
    ledger = settled_once(database, a.owner)
    assert "extraction-abandoned" in {row["purpose"] for row in ledger}
    [job] = jobs_of(database, a.owner)
    assert (job["state"], job["attempt_count"]) == ("succeeded", 2)


# ---- 7: two readers -------------------------------------------------------------------------------

def test_two_readers_story_memory_jobs_memory_and_spend_never_touch(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The break it catches: one reader's job reading or writing the other's rows (both have the
    same book file), another reader's rejections counted towards this reader's chapter, a second
    reader learning of or adding (and paying for) a job on the first reader's book, through the
    routes, the row-security role alone, or the queue's own predicates alone."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch, reject={2})
    a, book_a = opened(memory_app, readers, "a")
    b, book_b = opened(memory_app, readers, "b")
    a.mark(book_a, [1, 2, 3])
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 5
    a_rows, a_jobs, a_spend = (memory_of(database, a.owner), jobs_of(database, a.owner),
                               spend_of(database, a.owner))
    assert built(database, a.owner) == [(1, "body"), (2, "unremembered"), (3, "body")]

    # B's part 2 passes its review: A's two rejections of the same file are A's alone.
    models.reject = set()
    b.mark(book_b, [1, 2, 3])
    assert drain(work) == 3
    assert built(database, b.owner) == [(1, "body"), (2, "body"), (3, "body")]
    assert (memory_of(database, a.owner), jobs_of(database, a.owner),
            spend_of(database, a.owner)) == (a_rows, a_jobs, a_spend)
    with psycopg.connect(database) as conn:
        for table in ("jobs", "cost_reservations", "cost_ledger", "chapters", "entities",
                      "ingested_chapters", "entity_state", "events"):
            mixed = conn.execute(
                f"SELECT count(*) FROM {table} WHERE (owner_id=%s AND book_id<>%s)"
                " OR (owner_id=%s AND book_id<>%s)",
                (a.owner, uuid.UUID(book_a), b.owner, uuid.UUID(book_b))).fetchone()[0]
            assert mixed == 0, table

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
    assert (memory_of(database, a.owner), jobs_of(database, a.owner)) == (a_rows, a_jobs)


# ---- 8: erasure, removal and a copy, with these jobs present ---------------------------------------

def test_a_copy_removal_and_erasure_still_work_with_story_memory_jobs_and_rows(
        memory_app, readers, database, admin_dsn, worker_dsn, monkeypatch):
    """The break it catches: story-memory jobs (finished, failed on a chapter, waiting) or their
    rows that stop a copy of one's data, block removing the book, or survive erasing the
    account; or a copy that hands out story memory rows (ADR 0042 section 7: only the view at
    the reader's place, which is not built yet)."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch, reject={2})
    a, book = opened(memory_app, readers)
    a.mark(book, [1, 2])
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 4                               # 1, 2 rejected twice, 2 unremembered
    assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book)   # waiting
    second = upload(a.client, a.tokens, "a", lamps())
    a.mark(second, [1])                                   # one more, left waiting
    assert {row["state"] for row in jobs_of(database, a.owner)} == {
        "succeeded", "failed", "pending"}

    files = copy_of(a.call("GET", "/api/account/export"))
    everything = "\n".join(files.values())
    assert "Lamplighter" not in everything and "Corvus moved the lamps" not in everything

    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    assert not built(database, a.owner)
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


# ---- 9: switched off ------------------------------------------------------------------------------

@pytest.mark.parametrize("settings", [
    {**CHAT, "hosted_story_memory": True},                # the library's memory only (part 5)
    {**CHAT},                                             # story memory off altogether
])
def test_with_a_readers_own_story_memory_off_nothing_is_made_or_spent(
        tenant_dsn, readers, database, worker_dsn, tmp_path, monkeypatch, settings):
    """The break it catches: a job made, or money spent, for a reader's own book while the owner
    has not turned per-reader memory on; a story-memory job left from before run anyway; or the
    kind reachable at all with story memory off."""
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **settings)
    with client:
        extraction = extraction_client()
        models = Lamps(extraction, monkeypatch)
        a = Reader(client, tokens, "a", readers["a"])
        book = upload(client, tokens, "a", lamps())
        a.read(book, 1, 1, 60)
        a.mark(book, [2, 3])
        assert jobs_of(database, a.owner) == []
        # A job left from before (or planted): with the kind on, it runs and spends nothing; off,
        # no worker of this deployment takes it.
        assert StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book)
        if settings.get("hosted_story_memory"):
            assert drain(worker(app, extraction, worker_dsn)) == 1, (
                jobs_of(database, a.owner), rows(database, "SELECT * FROM provider_pauses"),
                rows(database, "SELECT now() AS now"))
            assert [row["state"] for row in jobs_of(database, a.owner)] == ["succeeded"]
        else:
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
        assert built(database, a.owner) == []


def test_story_memory_jobs_are_well_formed_or_refused_by_the_schema(memory_app, readers,
                                                                    database):
    """The break it catches: a story-memory job naming no book or carrying a credential, a
    second one waiting for the same book inserted around the queue, or a failure code outside
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


def test_with_story_memory_on_an_upload_makes_no_reader_key_extraction_job(
        tenant_dsn, readers, database, tmp_path):
    """ADR 0042 section 6: the reader-key extraction job (``ingest_book``), which never had a
    handler, is retired with story memory on, even where reader keys are on."""
    for keys, made in ((True, 0), (False, 0)):
        client, tokens, _app = memory_app_for(tenant_dsn, readers, tmp_path / str(keys),
                                              **MEMORY, hosted_reader_keys=keys)
        with client:
            upload(client, tokens, "a", lamps())
        assert len(jobs_of(database, readers["a"], "ingest_book")) == made
    client, tokens, _app = memory_app_for(tenant_dsn, readers, tmp_path / "old", **CHAT,
                                          hosted_reader_keys=True)
    with client:
        upload(client, tokens, "b", lamps())
    assert len(jobs_of(database, readers["b"], "ingest_book")) == 1     # as before, story memory off


# ---- the screens show what the jobs build (parts 3 and 4 together) -----------------------------

def test_the_story_screens_show_what_the_jobs_built_and_only_up_to_the_readers_place(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """The break it catches: the screens of part 4 working on hand-written rows but not on the
    rows the jobs really publish; a person of a chapter the reader has not finished on the cast;
    a cast that stays when a chapter is marked not read; or one reader seeing another's."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers)

    def screen(reader, path):
        return reader.call("GET", f"/api/books/{book}/{path}")

    # Nothing built and nothing asked for yet: the panels' "not yet" answer.
    assert screen(a, "graph").status_code == 404

    a.mark(book, [1, 2])                                  # parts 1 and 2 read; part 3 is not
    assert drain(worker(app, extraction, worker_dsn)) == 2
    assert built(database, a.owner) == [(1, "body"), (2, "body")]

    graph = screen(a, "graph").json()
    assert graph["as_of_chapter"] == 2
    assert sorted(person["canonical_name"] for person in graph["characters"]) == ["Corvus", "Mara"]
    notes = screen(a, "notes").json()
    assert sorted((chapter["revealed_at"], chapter["summary"]) for chapter in notes["chapters"]) == [
        (1, "Mara moved the lamps."), (2, "Corvus moved the lamps.")]
    progress = screen(a, "ingest").json()
    assert (progress["ingest_progress"], progress["status"]) == (2, "idle")
    # Part 3's people are nowhere: not built, and asking for a later chapter changes nothing.
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
    assert built(database, a.owner) == [(1, "body"), (2, "body")]

    # The second reader has nothing of it, by any route.
    b = Reader(_client, _tokens, "b", readers["b"])
    for path in ("graph", "notes", "ingest", f"character/{corvus['entity_id']}"):
        assert screen(b, path).status_code == 404
