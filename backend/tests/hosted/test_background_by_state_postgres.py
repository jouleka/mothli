"""Mothli's background work for a reader follows where they stand with their plan (ADR 0045
section 5, step 6), on real PostgreSQL.

In their free weeks and while they pay, chapter notes, recollections and story memory run as
before. Once their free weeks are over and they do not pay (ended: the two weeks passed, the free
ceiling reached, or a plan that stopped), none: the claim finishes such a reader's companion jobs
without work and without spending, as a cancelled job is, and the routes stop asking for new
ones; their reading is still recorded. When they pay again, their next question asks for the
book's story memory (and the reply for its chapter notes), and both build what is missing up to
the reader's place, as the read map gives it, and never past it. A reader who has signed up and
never asked anything gets a bounded warm-up: background work within ``HOSTED_WARMUP_DAYS`` of
sign-up, up to ``HOSTED_WARMUP_NEW_CHAPTERS`` chapters of story memory their reading is the first
to build and ``HOSTED_WARMUP_NOTES_USD`` of chapter notes; past any of the three it stops,
silently, and their first question asks again. The guards of ADR 0044 still hold on top. One
reader's state never changes what is built or served for another reader of the same file, and
nothing about any of it is shown to a reader.

Through the hosted HTTP app on the RLS-enforced tenant role and the worker on a role granted
exactly the reviewed lists. Plan states are set as the plan tests set them: the free weeks'
start moved back, the reader's own chat at the free ceiling, a gift, a Paddle plan's status.
Stand-in writer, checker, extraction and Jev only: nothing is called and nothing paid.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest

from app.hosted.companion.jobs import CompanionJobs
from app.hosted.jobs import PostgresWorkerRepository
from app.hosted.worker import DurableWorker
from tests.hosted import test_companion_notes_postgres as notes_gate
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted import test_story_memory_jobs_postgres as memory_gate
from tests.hosted.test_companion_notes_postgres import NotesModels, kept, notes_of, spend_on_ledger
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    CHAT,
    StandIns,
    restore_caps,
    sentences,
)
from tests.hosted.test_free_weeks_postgres import chat_spent, move_start, stamp
from tests.hosted.test_story_memory_jobs_postgres import (
    TOMORROW,
    Lamps,
    built_for,
    memory_app_for,
    opened,
    rows,
)
from tests.hosted.test_story_memory_one_ahead_postgres import handed, seed_first_builds
from tests.hosted.test_story_memory_postgres import edition_of, notes_spend, spend_of
from tests.parity.test_story_memory_parity import extraction_client

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
worker_dsn = jobs_gate.worker_dsn
notes_app = notes_gate.notes_app
memory_app = memory_gate.memory_app
line = read_map_gate.line

# Both kinds of a book's background work at once: chapter notes and story memory.
BOTH = {**CHAT, "reader_story_notes": True, "hosted_story_memory": True}


@pytest.fixture()
def shelf(tenant_dsn: str, readers: dict, tmp_path):
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **BOTH)
    with client:
        yield client, tokens, app


def worker(app, extraction, dsn: str) -> DurableWorker:
    """The worker as ``from_environment`` builds it with chapter notes and story memory on."""
    handler = CompanionJobs(tenant_dsn=app.state.companion_dsn, settings=app.state.settings,
                            client=app.state.platform_client, storage=app.state.object_storage,
                            sources=app.state.companion_sources,
                            kinds=frozenset({"chapter_notes", "story_memory"}),
                            extraction_client=extraction, lease_for=timedelta(seconds=60))
    return DurableWorker(PostgresWorkerRepository(dsn), handler, "state-worker",
                         lease_for=timedelta(seconds=60), retry_after=timedelta(0))


def drain(work: DurableWorker, most: int = 30) -> int:
    ran = 0
    while ran < most and work.run_once():
        ran += 1
    return ran


def handled(monkeypatch) -> list:
    """Every job the worker's handler is given, as (owner, kind): a job finished at the claim
    never reaches it, so nothing of its book is read, no source opened and no model asked."""
    ran = []
    real = CompanionJobs.__call__

    def recorded(self, claim, repository):
        ran.append((claim.owner_id, claim.kind))
        return real(self, claim, repository)

    monkeypatch.setattr(CompanionJobs, "__call__", recorded)
    return ran


def jobs(dsn: str, owner: uuid.UUID) -> list[dict]:
    return rows(dsn, f"SELECT * FROM jobs WHERE owner_id=%s {jobs_gate.JOB_ORDER}", owner)


def waiting(dsn: str, owner: uuid.UUID) -> list[str]:
    return sorted(job["kind"] for job in jobs(dsn, owner) if job["state"] == "pending")


def state_of(dsn: str, owner: uuid.UUID) -> str:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state FROM reader_plan_state(%s, 14, 2.10)",
                            (owner,)).fetchone()[0]


def subscription(owner: uuid.UUID) -> str:
    return "sub_" + owner.hex[:26]


def paddle(dsn: str, owner: uuid.UUID, status: str) -> None:
    """A Paddle plan of this status for the reader, as an applied message leaves the row."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """INSERT INTO reader_plans (owner_id,paddle_subscription_id,paddle_status,
                                         paddle_occurred_at)
               VALUES (%s,%s,%s,now())
               ON CONFLICT (owner_id) DO UPDATE
               SET paddle_subscription_id=EXCLUDED.paddle_subscription_id,
                   paddle_status=EXCLUDED.paddle_status,
                   paddle_occurred_at=EXCLUDED.paddle_occurred_at""",
            (owner, subscription(owner), status))


def end(database: str, tenant: str, owner: uuid.UUID, why: str) -> None:
    """Make the reader ended: their two weeks passed, their own chat at the free ceiling, or a
    Paddle plan that stopped."""
    if why == "plan":
        paddle(database, owner, "canceled")
    else:
        stamp(tenant, owner)
        if why == "time":
            move_start(database, owner, "15 days")
        else:
            chat_spent(database, owner, Decimal("2.10"))
    assert state_of(database, owner) == "ended"


def pay(database: str, owner: uuid.UUID, how: str) -> None:
    """Make the reader paying: a gift for good (``set_gift``, as the operator's command calls
    it), or their Paddle plan live again."""
    with psycopg.connect(database) as conn:
        if how == "gift":
            assert conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (owner,)).fetchone()[0]
    if how == "paddle":
        paddle(database, owner, "active")
    assert state_of(database, owner) == "paying"


def signed_up(database: str, owner: uuid.UUID, days_ago: int) -> None:
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE users SET created_at=now()-make_interval(days=>%s::integer)"
                     " WHERE id=%s",
                     (days_ago, owner))


def nothing_done_for(database: str, owner: uuid.UUID, book: str) -> None:
    """Every job of the reader finished without work: cancelled at the claim, never attempted,
    nothing spent by them or by the notes account, no notes, no edition made."""
    found = jobs(database, owner)
    assert found and all((job["state"], job["attempt_count"]) == ("cancelled", 0)
                         and job["completed_at"] is not None for job in found), found
    assert rows(database, "SELECT * FROM job_attempts WHERE owner_id=%s", owner) == []
    assert spend_of(database, owner)[0] == [] and notes_spend(database) == ([], [])
    assert not [row for row in spend_of(database, owner)[1] if row["background"]]
    assert notes_of(database, owner) == [] and edition_of(database, owner, book) is None


# ---- ended: nothing, silently ----------------------------------------------------------------------

@pytest.mark.parametrize("why", ["time", "ceiling", "plan"])
def test_an_ended_readers_jobs_are_finished_at_the_claim_and_no_more_are_asked_for(
        shelf, readers, database, tenant_dsn, worker_dsn, monkeypatch, why):
    """ADR 0045 section 5, "Ended: none, silently". The break it catches: an ended reader's
    chapter notes or story memory built or paid for (by them or the notes account); their jobs
    left waiting for ever, failed, retried or claimed again; the handler given them at all (so a
    source opened or a chapter read); a page or "I've read this" asking for new jobs; or their
    reading no longer recorded."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    lamps = Lamps(extraction, monkeypatch)
    notes = NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a, book = opened(shelf, readers)                      # a page read: story memory asked for
    a.mark(book, [1])                                     # and part 1's notes
    assert waiting(database, a.owner) == ["chapter_notes", "story_memory"]
    end(database, tenant_dsn, a.owner, why)

    ran = handled(monkeypatch)
    assert drain(worker(app, extraction, worker_dsn)) == 0
    assert ran == [] and lamps.calls == [] and notes.notes_prompts == []
    nothing_done_for(database, a.owner, book)

    # Reading is recorded as before; asking for Mothli's background work is not.
    ranges = rows(database, "SELECT count(*) AS n FROM read_ranges WHERE owner_id=%s", a.owner)
    a.read(book, 2, 1, 30)
    a.mark(book, [2])
    a.read(book, 3, 1, 5)
    assert rows(database, "SELECT count(*) AS n FROM read_ranges WHERE owner_id=%s",
                a.owner)[0]["n"] > ranges[0]["n"]
    assert waiting(database, a.owner) == []
    assert drain(worker(app, extraction, worker_dsn)) == 0 and ran == []
    nothing_done_for(database, a.owner, book)


def test_work_that_began_before_the_reader_ended_is_finished(
        notes_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0045 section 5, the rule for a reply under way applied to a job: the claim decides
    whether a run happens; a run it let start is finished. The break it catches: notes for the
    rest of the run thrown away, or the run failing, because the reader's free weeks ended while
    it ran; or a further job asked for afterwards."""
    _client, _tokens, app = notes_app
    a, book = notes_gate.opened(notes_app, readers)
    a.mark(book, [2])                                     # one job: parts 1 and 2
    stamp(tenant_dsn, a.owner)

    def the_free_weeks_end_mid_run():
        if len(models.notes_prompts) == 1:
            move_start(database, a.owner, "15 days")

    models = NotesModels(app.state.platform_client, monkeypatch,
                         notes_hook=the_free_weeks_end_mid_run)
    assert drain(notes_gate.worker(app, worker_dsn)) == 1
    assert state_of(database, a.owner) == "ended"
    assert [(n["atom"], n["sentences"]) for n in notes_of(database, a.owner)] == [
        (1, kept(1)), (2, kept(2))]
    assert [job["state"] for job in notes_gate.jobs_of(database, a.owner)] == ["succeeded"]
    a.mark(book, [3])
    assert [job["state"] for job in notes_gate.jobs_of(database, a.owner)] == ["succeeded"]


# ---- paying again: both kinds resume, up to the place and never past it ----------------------------

@pytest.mark.parametrize("ended_by, how", [("time", "gift"), ("plan", "paddle")])
def test_a_reader_who_pays_again_gets_notes_and_story_memory_to_their_place_and_not_past_it(
        shelf, readers, database, tenant_dsn, worker_dsn, monkeypatch, ended_by, how):
    """ADR 0045 sections 5 and 9: subscribing picks up where the reader left off, and nothing is
    ever built or served past their place. The reader finished part 1 while ended (nothing was
    asked for or built); then they pay and ask a question. The break it catches: either kind not
    resuming; chapter notes written for a chapter not finished, or from its text; story memory
    built past the target the read map gives (one chapter past the last finished, ADR 0044) or
    the door handing them any of it past their place; or the finished-without-work jobs having
    read or written anything."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    lamps = Lamps(extraction, monkeypatch)
    models = NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    work = worker(app, extraction, worker_dsn)
    a, book = opened(shelf, readers)                      # story memory asked for while new
    end(database, tenant_dsn, a.owner, ended_by)
    a.mark(book, [1])                                     # place 1, ended: nothing asked for
    ran = handled(monkeypatch)
    assert drain(work) == 0 and ran == []
    nothing_done_for(database, a.owner, book)
    assert lamps.calls == [] and models.notes_prompts == [] and models.writes == 0

    pay(database, a.owner, how)
    assert sentences(a.ask(book)) == [line(1, 2)]         # the question asks for both again
    assert drain(work) >= 2
    assert {kind for _owner, kind in ran} == {"chapter_notes", "story_memory"}
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert lamps.parts() == [1, 2]                        # part 3 never read or built
    assert [(n["atom"], n["sentences"]) for n in notes_of(database, a.owner)] == [(1, kept(1))]
    assert models.notes_prompts and all("in part 2" not in prompt and "in part 3" not in prompt
                                        for prompt in models.notes_prompts)
    assert handed(tenant_dsn, a.owner, book) == [1]
    for path in ("graph", "graph?bookmark=99", "notes?bookmark=99"):
        shown = a.call("GET", f"/api/books/{book}/{path}")
        assert shown.status_code == 200 and "Corvus" not in shown.text, path
    assert not [job for job in jobs(database, a.owner) if job["state"] in ("failed", "pending")]


# ---- one reader's state never reaches another reader of the same file -------------------------------

def test_two_readers_of_one_file_one_ended_one_paying_each_get_only_what_their_own_state_gives(
        shelf, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0045 section 5 and the owner's rule that nothing shown may imply anything about another
    reader. A is ended, B pays; both have the same file and have finished part 1. The break it
    catches: B's notes or story memory held because A is ended (or A's jobs, claimed first,
    holding up B's); A given B's background work (chapter notes, or a job of A's run); or what
    either is served or shown differing because of the other's state: A is served the chapters
    B's reading built up to A's own place, as before this step, and the screens say the same to
    both."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    lamps = Lamps(extraction, monkeypatch)
    NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a, book_a = opened(shelf, readers, "a")
    b, book_b = opened(shelf, readers, "b")               # the same file
    end(database, tenant_dsn, a.owner, "time")
    pay(database, b.owner, "gift")
    a.mark(book_a, [1])
    b.mark(book_b, [1])
    ran = handled(monkeypatch)
    drain(worker(app, extraction, worker_dsn))

    assert {owner for owner, _kind in ran} == {b.owner}
    assert edition_of(database, a.owner, book_a) == edition_of(database, b.owner, book_b)
    assert built_for(database, b.owner, book_b) == [(1, "body"), (2, "body")]
    assert lamps.parts() == [1, 2]
    assert [n["atom"] for n in notes_of(database, b.owner)] == [1]
    assert all(job["state"] == "succeeded" for job in jobs(database, b.owner))
    assert all(job["state"] == "cancelled" and job["attempt_count"] == 0
               for job in jobs(database, a.owner))
    assert notes_of(database, a.owner) == []
    assert not [row for row in spend_of(database, a.owner)[1] if row["background"]]

    # What each is served and shown comes from their own place alone.
    assert handed(tenant_dsn, a.owner, book_a) == [1] == handed(tenant_dsn, b.owner, book_b)
    for path in ("graph", "ingest"):
        seen = [someone.call("GET", f"/api/books/{book}/{path}") for someone, book in (
            (a, book_a), (b, book_b))]
        assert [response.status_code for response in seen] == [200, 200], path
        assert seen[0].text.replace(book_a, "BOOK") == seen[1].text.replace(book_b, "BOOK"), path
        assert "Corvus" not in seen[0].text


# ---- new: the warm-up, within each bound and not past it ------------------------------------------

@pytest.mark.parametrize("days_ago, built", [(29, True), (31, False)])
def test_a_new_reader_is_built_for_within_the_warm_ups_days_and_not_after(
        shelf, readers, database, worker_dsn, monkeypatch, days_ago, built):
    """ADR 0045 section 5, the warm-up's first bound: 30 days from sign-up. The break it catches:
    a reader who signed up a month ago and never asked having story memory or notes built for
    them, or one inside the 30 days left cold; or, past it, their first question not asking for
    the work again (it starts their free weeks)."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    lamps = Lamps(extraction, monkeypatch)
    models = NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    work = worker(app, extraction, worker_dsn)
    a, book = opened(shelf, readers)
    a.mark(book, [1])
    signed_up(database, a.owner, days_ago)
    assert state_of(database, a.owner) == "new"
    drain(work)
    if built:
        assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
        assert [n["atom"] for n in notes_of(database, a.owner)] == [1]
        return
    nothing_done_for(database, a.owner, book)
    assert lamps.calls == [] and models.notes_prompts == []

    assert sentences(a.ask(book)) == [line(1, 2)]         # the first question: free weeks
    assert state_of(database, a.owner) == "free"
    drain(work)
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert [n["atom"] for n in notes_of(database, a.owner)] == [1]


def test_a_new_reader_gets_chapter_notes_up_to_the_warm_ups_amount_and_not_past_it(
        notes_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0045 section 5, the warm-up's second bound: $0.50 of chapter notes, counted from the
    reader's background ledger over the whole time they have not asked. The break it catches: a
    new reader under it held, or one at it given more notes (or a job of theirs left waiting, or
    held only until tomorrow and paid for then)."""
    _client, _tokens, app = notes_app
    models = NotesModels(app.state.platform_client, monkeypatch)
    a, book = notes_gate.opened(notes_app, readers)       # part 1's notes asked for
    spend_on_ledger(database, a.owner, book, "0.49")      # earlier notes, just under the bound
    assert drain(notes_gate.worker(app, worker_dsn)) == 1
    assert [(n["atom"], n["sentences"]) for n in notes_of(database, a.owner)] == [(1, kept(1))]

    spend_on_ledger(database, a.owner, book, "0.01")      # now at or past it
    notes_gate.finish_chapter_two(a, book)                # asked for, as for every new reader
    assert [job["state"] for job in notes_gate.jobs_of(database, a.owner)] == [
        "succeeded", "pending"]
    prompts = len(models.notes_prompts)
    assert drain(notes_gate.worker(app, worker_dsn)) == 0
    assert [(job["state"], job["attempt_count"]) for job in notes_gate.jobs_of(
        database, a.owner)] == [("succeeded", 1), ("cancelled", 0)]
    assert [n["atom"] for n in notes_of(database, a.owner)] == [1]
    assert len(models.notes_prompts) == prompts


def test_a_new_reader_is_first_to_the_warm_ups_chapters_and_not_one_more(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0045 section 5, the warm-up's third bound: 25 chapters of story memory their reading
    is the first to build, counted over the whole time they are new (their jobs that published,
    kept for the life of the account). The break it catches: the 25th chapter refused, a 26th
    built, the reader's later runs waiting for tomorrow and paying then, or their first question
    not resuming the work."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    lamps = Lamps(extraction, monkeypatch)
    work = memory_gate.worker(app, extraction, worker_dsn)
    _seeded, seed_book = opened(memory_app, readers, title="Seeded", reading=False)
    seed_first_builds(database, readers["a"], seed_book, 24, ended="now() - interval '20 days'")
    a, book = opened(memory_app, readers)                 # a page read: target 1
    a.mark(book, [1])                                     # target 2
    assert drain(work) == 1                               # part 1, the 25th; part 2 not run
    assert built_for(database, a.owner, book) == [(1, "body")]
    assert lamps.parts() == [1]
    last = [job for job in jobs(database, a.owner) if str(job["book_id"]) == book][-1]
    assert (last["state"], last["attempt_count"]) == ("cancelled", 0)
    assert waiting(database, a.owner) == []

    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    assert sentences(a.ask(book)) == [line(1, 2)]         # the first question: free weeks
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert lamps.parts() == [1, 2]


def test_the_chapter_guards_still_hold_on_top_of_the_warm_up(
        tenant_dsn, readers, database, worker_dsn, tmp_path, monkeypatch):
    """ADR 0044 section 5's guards are not replaced by the warm-up. The break it catches: a new
    reader inside the warm-up let past the day's guard for new chapters (here one a day), or
    their held work finished for good rather than waiting for the next UTC day."""
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **memory_gate.MEMORY,
                                         hosted_story_memory_new_chapters_per_day=1)
    with client:
        shelf_ = (client, tokens, app)
        extraction = extraction_client()
        lamps = Lamps(extraction, monkeypatch)
        _seeded, seed_book = opened(shelf_, readers, title="Seeded", reading=False)
        seed_first_builds(database, readers["a"], seed_book, 1)
        a, book = opened(shelf_, readers)
        assert state_of(database, a.owner) == "new"
        assert drain(memory_gate.worker(app, extraction, worker_dsn)) == 1
        assert lamps.calls == [] and edition_of(database, a.owner, book) is None
        last = jobs(database, a.owner)[-1]
        assert (last["state"], last["run_after"]) == ("pending", rows(database, TOMORROW)[0]["at"])


# ---- the free weeks and paying readers: as before -----------------------------------------------

@pytest.mark.parametrize("standing", ["free", "paying"])
def test_the_warm_up_never_holds_a_reader_in_their_free_weeks_or_paying(
        shelf, readers, database, tenant_dsn, worker_dsn, monkeypatch, standing):
    """ADR 0045 section 5: "Free weeks, Paying: as today". The break it catches: the warm-up's
    bounds applied to a reader who has asked (or pays): one who signed up long ago, whose notes
    have cost more than the warm-up's amount and whose reading was first to more chapters than
    its count, held."""
    _client, _tokens, app = shelf
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    NotesModels(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a, book = opened(shelf, readers)
    _seeded, seed_book = opened(shelf, readers, title="Seeded", reading=False)
    seed_first_builds(database, a.owner, seed_book, 26, ended="now() - interval '20 days'")
    spend_on_ledger(database, a.owner, book, "0.60")
    signed_up(database, a.owner, 400)
    if standing == "free":
        stamp(tenant_dsn, a.owner)
    else:
        pay(database, a.owner, "gift")
    assert state_of(database, a.owner) == standing
    a.mark(book, [1])
    drain(worker(app, extraction, worker_dsn))
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert [n["atom"] for n in notes_of(database, a.owner)] == [1]
    assert not [job for job in jobs(database, a.owner) if job["state"] == "cancelled"]
