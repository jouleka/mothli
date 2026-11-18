"""Story memory for every book, one chapter ahead, paid by Mothli (ADR 0044 step 4), against real
PostgreSQL.

Every hosted reader's book asks for its story-memory job when a page finishes a chapter, on "I've
read this", and on the first page of the book read (not skimmed); there is no setting for a
reader's own books any more. A run builds the edition of the reader's file up to one chapter past
the last chapter they have finished, never past the book's end, and nothing at all before they
have read a page of it: adding a file, skimming it, or marking chapters read without reading a
page builds and spends nothing. Every call is paid by the notes account against the edition:
no reader's allowance is read or moved, a reader at their chat cap still gets their notes, and a
reader past a notes guard still chats. One reader's runs may be the first to build at most 30
new chapters a UTC day and 300 a month, and be first to chapter 1 of at most 5 new files a
month, counted from their own jobs that published; past a guard, or at the notes ceiling, the
run spends nothing and waits whole for the next UTC day, and chapters someone else built never
count. The abuse case of section 5 is run as written.

The two spoiler properties of one ahead are proven here: a run for a reader who has finished
chapter n reads the edition's notes only below the chapter it builds (so through n), may ask only
whether chapter n+1 exists or is unremembered, and reads nothing of n+2 or later; and the door
hands that reader nothing of chapter n+1 after their own run built it (raw function, screens,
the chat's memory and everything its writer and checker are given).

Stand-in writer, checker and Jev only: nothing is called and nothing paid.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.hosted.companion.jobs import StoryMemoryQueue, story_memory_target
from app.hosted.companion.memory import HostedBookStore
from app.hosted.companion.source import HostedBookSource
from app.hosted.companion.spend import _DAY
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.jobs import PostgresWorkerRepository
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_jobs_postgres as jobs_gate
from tests.hosted import test_story_memory_jobs_postgres as memory_gate
from tests.hosted.test_companion_stream_postgres import (  # noqa: F401  (autouse fixture)
    Reader,
    StandIns,
    restore_caps,
    sentences,
)
from tests.hosted.test_library_story_memory_postgres import PARTS, Writer, long_lamps, tpage
from tests.hosted.test_story_memory_jobs_postgres import (
    MEMORY,
    TOMORROW,
    Lamps,
    built_for,
    drain,
    jobs_of,
    memory_app_for,
    opened,
    rows,
    worker,
)
from tests.hosted.test_story_memory_postgres import (
    edition_of,
    notes_account,
    notes_spend,
    settled_once,
    spend_of,
)
from tests.parity.test_story_memory_parity import extraction_client

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
worker_dsn = jobs_gate.worker_dsn
memory_app = memory_gate.memory_app
line = read_map_gate.line
upload = read_map_gate.upload

MONTH_START = "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


def book_jobs(dsn: str, owner: uuid.UUID, book) -> list[dict]:
    """One reader's story-memory jobs of one book, oldest first."""
    return [row for row in jobs_of(dsn, owner) if str(row["book_id"]) == str(book)]


def seed_first_builds(dsn: str, owner: uuid.UUID, book, count: int, *, chapter: int = 2,
                      ended: str = "now()") -> None:
    """``count`` story-memory jobs of this reader's that published a chapter (were the first to
    build it), each of chapter ``chapter`` of an edition of its own, ended at ``ended``: as the
    reader's earlier runs leave them (``commit_chapter_memory`` marks the job)."""
    with psycopg.connect(dsn) as conn:
        [(incarnation,)] = conn.execute("SELECT incarnation FROM books WHERE owner_id=%s"
                                        " AND id=%s", (owner, book)).fetchall()
        for _ in range(count):
            conn.execute(
                "INSERT INTO jobs (owner_id,book_id,book_incarnation,kind,idempotency_key,state,"
                "payload_metadata,attempt_count,completed_at) VALUES (%s,%s,%s,'story_memory',%s,"
                f"'succeeded',%s,1,{ended})",
                (owner, book, incarnation, f"story-memory:seeded-{uuid.uuid4().hex}",
                 Jsonb({"chapter": chapter, "edition": str(uuid.uuid4()), "published": True})))


def door_rows(dsn: str, owner, book) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute("SELECT * FROM story_memory_door(%s)", (book,)).fetchall()


def handed(dsn: str, owner, book) -> list[int]:
    """The chapters the door hands this reader anything of."""
    [row] = door_rows(dsn, owner, book)
    return sorted(item["ordinal"] for item in row["completed"])


def tomorrow(dsn: str):
    return rows(dsn, TOMORROW)[0]["at"]


# ---- 1: the trigger and the target ------------------------------------------------------------------

def test_the_target_is_one_chapter_past_the_last_finished_and_never_past_the_book(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 section 3, "one chapter ahead of the reader, decided". The break it catches: a
    run that builds more than one chapter past the last finished one (paying for chapters nobody
    reached), builds nothing ahead (the reader waits on every chapter), builds past the book's
    last chapter, or builds before the reader has read a page; the first page read, or a
    finished chapter, not asking for the job of any reader's book (the setting that held uploads
    back is gone); or another page turn asking again."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, reading=False)
    work = worker(app, extraction, worker_dsn)
    assert jobs_of(database, a.owner) == []
    a.read(book, 1, 1, 10)                                # the first page read asks: target 1
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"]
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body")]
    a.read(book, 1, 11, 20)                               # another page, finishing nothing
    assert len(jobs_of(database, a.owner)) == 1
    a.mark(book, [1])                                     # part 1 finished: target 2
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    a.mark(book, [2])                                     # target 3, the last part
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body"), (3, "body")]
    a.mark(book, [3])                                     # one past the last part: still 3
    assert drain(work) == 1
    assert models.parts() == [1, 2, 3]
    assert jobs_of(database, a.owner)[-1]["payload_metadata"] == {}   # named no chapter

    # The target itself, as the run computes it from the reader's own rows.
    catalog = HostedCompanionCatalog(app.state.companion_dsn, OwnerId(a.owner))
    source = HostedBookSource(app.state.object_storage, catalog, app.state.companion_sources)
    settings = app.state.settings

    def target():
        return story_memory_target(book, catalog, settings, source)

    assert target() == 3
    a.mark(book, [2, 3], read=False)                      # part 1 finished, a page read
    assert target() == 2
    a.mark(book, [1], read=False)                         # nothing read at all any more
    assert target() == 0
    a.mark(book, [1, 2, 3])                               # all marked read, no page read
    assert target() == 0
    a.read(book, 3, 1, 5)                                 # one page read: one past part 3 is 3
    assert target() == 3
    with psycopg.connect(database) as conn:               # a counter pushed ahead moves nothing
        conn.execute("UPDATE reading_state SET bookmark=9,memory_through=9"
                     " WHERE owner_id=%s AND book_id=%s", (a.owner, book))
    a.mark(book, [3], read=False)
    a.read(book, 1, 1, 5)
    assert target() == 3


def test_an_upload_a_skimmed_page_and_chapters_marked_read_without_a_read_page_build_and_spend_nothing(  # noqa: E501
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 sections 3 and 5: "importing a file costs nothing", and a skimmed page finishes
    and asks for nothing. Takes the place of the step 3 test "with a reader's own story memory
    off nothing is made or spent", whose setting is gone. The break it catches: an upload that
    asks for or builds anything; a skimmed page that asks; chapters marked read ("I've read
    this", or reading imported from elsewhere) on a book with no page read building, making an
    edition or spending anything, for the reader or the notes account."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers, reading=False)
    work = worker(app, extraction, worker_dsn)
    assert rows(database, "SELECT id FROM jobs WHERE owner_id=%s", a.owner) == []
    a.read(book, 1, 1, 60, skimmed=True)
    a.read(book, 2, 1, 60, skimmed=True)
    assert jobs_of(database, a.owner) == []               # skimmed pages ask for nothing
    a.mark(book, [1, 2, 3])                               # "I've read this", no page read
    imported = a.call("PUT", f"/api/books/{book}/read-chapters",
                      {"atoms": [1, 2], "read": True, "source": "import"})
    assert imported.status_code == 200, imported.text
    assert [row["state"] for row in jobs_of(database, a.owner)] == ["pending"]
    assert drain(work) == 1
    assert [(row["state"], row["payload_metadata"]) for row in jobs_of(database, a.owner)] == [
        ("succeeded", {})]
    assert models.calls == [] and edition_of(database, a.owner, book) is None
    assert spend_of(database, a.owner) == ([], []) == notes_spend(database)

    # A page read, not skimmed: from then on one past what is finished (all three parts).
    a.read(book, 1, 1, 5)
    assert drain(work) == 3
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body"), (3, "body")]
    assert spend_of(database, a.owner) == ([], [])


# ---- 2: the guards ------------------------------------------------------------------------------------

@pytest.mark.parametrize("guard, limit, settings, ended", [
    ("day", 30, {}, "now()"),
    ("month", 300, {"hosted_story_memory_new_chapters_per_day": 100000}, MONTH_START),
])
def test_a_reader_past_a_chapter_guard_waits_whole_for_tomorrow_and_others_builds_never_count(
        tenant_dsn, readers, database, worker_dsn, tmp_path, monkeypatch, guard, limit, settings,
        ended):
    """ADR 0044 section 5, with the owner's numbers: a reader's runs may be the first to build
    at most 30 new chapters a UTC day and 300 a month. The break it catches: a run past a guard
    that spends anything, makes an edition, names a chapter, or does not wait for the next UTC
    day; a guard counted from anything but the reader's own runs that published (so a chapter
    another reader built counting, or holding a reader whose next chapters are built); or a
    guard one off (the reader at limit - 1 must still get their chapter)."""
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **MEMORY, **settings)
    with client:
        shelf = (client, tokens, app)
        extraction = extraction_client()
        models = Lamps(extraction, monkeypatch)
        repository = PostgresWorkerRepository(worker_dsn)
        a, book = opened(shelf, readers)                  # a page read: target 1
        _seeded, seed_book = opened(shelf, readers, title="Seeded", reading=False)
        seed_first_builds(database, a.owner, seed_book, limit - 1, ended=ended)
        a.mark(book, [1])                                 # target 2
        work = worker(app, extraction, worker_dsn)
        assert drain(work) == 2                           # part 1 (the limit-th), then held
        assert built_for(database, a.owner, book) == [(1, "body")]
        assert models.parts() == [1]
        built = repository.first_builds(a.owner)
        assert built[guard] == limit
        *_done, held, waiting = book_jobs(database, a.owner, book)
        assert (held["state"], held["payload_metadata"]) == ("succeeded", {})
        assert (waiting["state"], waiting["attempt_count"], waiting["run_after"]) == (
            "pending", 0, tomorrow(database))
        assert {row["chapter_ordinal"] for row in notes_spend(database)[1]} == {1}
        assert spend_of(database, a.owner) == ([], [])
        # What the reader's screens say: the job waits for tomorrow, as any capped one did.
        progress = a.call("GET", f"/api/books/{book}/ingest")
        assert progress.status_code == 200 and progress.json()["status"] == "waiting"

        # B reads the same file and builds parts 2 and 3: B's runs, not A's.
        b, book_b = opened(shelf, readers, "b")
        b.mark(book_b, [1, 2])
        assert drain(work) == 2
        assert built_for(database, a.owner, book) == [(1, "body"), (2, "body"), (3, "body")]
        assert repository.first_builds(a.owner) == built
        assert repository.first_builds(b.owner)["day"] == 2
        # A's job runs again: everything up to A's target is built, so it builds and spends
        # nothing, and is not held (no guard is met by a run that would be first to nothing).
        spent = notes_spend(database)
        with psycopg.connect(database) as conn:
            conn.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (waiting["id"],))
        assert drain(work) == 1
        assert [row["state"] for row in book_jobs(database, a.owner, book)][-1] == "succeeded"
        assert notes_spend(database) == spent and models.parts() == [1, 2, 3]
        assert handed(tenant_dsn, a.owner, book) == [1]   # A has finished part 1


def test_past_five_new_files_a_month_a_new_file_waits_and_a_file_others_began_does_not(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """ADR 0044 section 5: a reader's runs may be the first to chapter 1 of at most 5 new files a
    month. The break it catches: a sixth new file begun (an edition made, anything spent); the
    guard holding a file whose chapter 1 another reader built (it is not a new file for this
    reader); or counting chapters that are not a chapter 1."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    models = Lamps(extraction, monkeypatch)
    repository = PostgresWorkerRepository(worker_dsn)
    a, book = opened(memory_app, readers)                 # a page read: target 1
    _seeded, seed_book = opened(memory_app, readers, title="Seeded", reading=False)
    seed_first_builds(database, a.owner, seed_book, 5, chapter=1)
    work = worker(app, extraction, worker_dsn)
    assert drain(work) == 1                               # held: a sixth new file this month
    assert edition_of(database, a.owner, book) is None and models.calls == []
    assert notes_spend(database) == ([], [])
    [waiting] = book_jobs(database, a.owner, book)[-1:]
    assert (waiting["state"], waiting["run_after"]) == ("pending", tomorrow(database))

    # B begins the same file; A's runs then build on it: it is not a new file for A.
    b, book_b = opened(memory_app, readers, "b")
    assert drain(work) == 1
    assert built_for(database, b.owner, book_b) == [(1, "body")]
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE jobs SET run_after=now() WHERE id=%s", (waiting["id"],))
    a.mark(book, [1])                                     # target 2
    assert drain(work) == 1
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert models.parts() == [1, 2]
    assert repository.first_builds(a.owner) == {"day": 6, "month": 6, "files": 5}
    assert handed(tenant_dsn, a.owner, book) == [1]


def test_the_abuse_case_many_files_read_through_is_bounded_by_the_guards(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 section 5, "the abuse case, worked through": someone imports many files and says
    "I've read this" through all of them (here seven long books of eleven parts, a page of each
    read). The break it catches: more than 30 new chapters built for one reader in a day, more
    than 5 new files begun in a month, anything paid by the reader, or anything spent for the
    held work. Without the guards this reader would have 77 chapters built."""
    client, tokens, app = memory_app
    extraction = extraction_client()
    writer = Writer(extraction, monkeypatch)
    a = Reader(client, tokens, "a", readers["a"])
    books = [upload(client, tokens, "a", long_lamps(f"The Long Lamp Book, Printing {n}"))
             for n in range(1, 8)]
    for book in books:
        page = a.call("PUT", f"/api/books/{book}/read-page", tpage(1, 1, 2))
        assert page.status_code == 200, page.text
        a.mark(book, list(range(1, PARTS + 1)))
    drain(worker(app, extraction, worker_dsn), 200)
    built = {book: built_for(database, a.owner, book) for book in books}
    begun = [book for book in books if built[book]]
    assert 1 <= len(begun) <= 5
    assert sum(len(found) for found in built.values()) == 30
    assert all(found == [(n, "body") for n in range(1, len(found) + 1)]
               for found in built.values())
    assert len(writer.parts()) == 30                      # each built chapter extracted once
    assert PostgresWorkerRepository(worker_dsn).first_builds(a.owner)["day"] == 30
    at = tomorrow(database)
    for book in books:                                    # every book's work waits for tomorrow
        assert [(row["state"], row["run_after"]) for row in book_jobs(
            database, a.owner, book) if row["state"] == "pending"] == [("pending", at)]
    account = notes_account(database)
    ledger = settled_once(database, account)
    editions = {edition_of(database, a.owner, book)[1] for book in begun}
    assert {row["book_id"] for row in ledger} == editions
    assert len({(row["book_id"], row["chapter_ordinal"]) for row in ledger}) == 30
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM books WHERE owner_id=%s AND edition_of IS NOT"
                            " NULL", (account,)).fetchone()[0] == len(begun)
    assert spend_of(database, a.owner) == ([], [])


# ---- 3: the notes ceiling ---------------------------------------------------------------------------

@pytest.mark.parametrize("ceiling", ["setting", "notes account's row"])
def test_at_the_notes_ceiling_every_new_chapter_waits_and_chapters_built_are_served_as_before(
        tenant_dsn, readers, database, worker_dsn, tmp_path, monkeypatch, ceiling):
    """ADR 0044 section 5: "At the ceiling every new chapter waits for tomorrow; chapters already
    built are served as before." The ceiling is HOSTED_STORY_MEMORY_USD_PER_DAY (a default in
    code, $10, so a fresh deployment needs no step), or the notes account's own day cap where
    the operator sets one. The break it catches: a chapter begun at the ceiling spending
    anything; a chapter admitted under it cut off half-way; a held run not waiting for the next
    UTC day; a built chapter no longer handed out; or the reader's chat held by the ceiling."""
    settings = ({"hosted_story_memory_usd_per_day": 0.001} if ceiling == "setting" else {})
    client, tokens, app = memory_app_for(tenant_dsn, readers, tmp_path, **MEMORY, **settings)
    with client:
        shelf = (client, tokens, app)
        if ceiling != "setting":
            update_limits(database, notes_account(database),
                          {"max_spend_usd_per_day": Decimal("0.001")})
        extraction = extraction_client()
        models = Lamps(extraction, monkeypatch, usage=(500, 200))   # $0.0015 a call at terra
        a, book = opened(shelf, readers)
        a.mark(book, [1])                                 # target 2
        work = worker(app, extraction, worker_dsn)
        assert drain(work) == 2                           # part 1 whole, then part 2 held
        assert built_for(database, a.owner, book) == [(1, "body")]
        ledger = settled_once(database, notes_account(database))
        assert sum(row["usd"] for row in ledger) > Decimal("0.001")
        assert {row["chapter_ordinal"] for row in ledger} == {1}
        waiting = book_jobs(database, a.owner, book)[-1]
        assert (waiting["state"], waiting["attempt_count"], waiting["run_after"]) == (
            "pending", 0, tomorrow(database))
        assert models.parts() == [1]

        # B, of the same file: part 1 is handed to B as before; part 2 waits for B too.
        b, book_b = opened(shelf, readers, "b")
        b.mark(book_b, [1])
        assert drain(work) == 1
        assert handed(tenant_dsn, b.owner, book_b) == [1]
        assert models.parts() == [1] and notes_spend(database)[1] == ledger
        assert book_jobs(database, b.owner, book_b)[-1]["run_after"] == tomorrow(database)

        # A's chat is A's: the notes ceiling holds none of it.
        chat = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
        assert sentences(a.ask(book)) == [line(1, 2)]
        assert chat.writes == 1
        assert notes_spend(database)[1] == ledger


# ---- 4: Mothli pays -----------------------------------------------------------------------------------

def test_notes_are_paid_by_the_notes_account_on_the_edition_and_a_readers_chat_is_their_own(
        memory_app, readers, database, worker_dsn, monkeypatch):
    """ADR 0044 section 5, option 2 and the owner's rule: "users should have their own quota for
    speaking with mothli, that's it." The break it catches: a chapter's calls reserved or
    settled against the reader (their day or month moved), against anything but the edition, or
    not counted in the platform's day; a reader at their chat cap whose notes are held (the
    claim's per-reader dollar check applied to story memory); a reader's chat cap lifted or
    used by notes; or a reader past every notes guard refused the chat."""
    _client, _tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch, usage=(500, 200))
    a, book = opened(memory_app, readers)
    update_limits(database, a.owner, {"max_spend_usd_per_day": Decimal("0")})   # chat used up
    a.mark(book, [1])                                     # target 2
    assert drain(worker(app, extraction, worker_dsn)) == 2
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert spend_of(database, a.owner) == ([], [])
    account, edition = edition_of(database, a.owner, book)
    assert account == notes_account(database)
    ledger = settled_once(database, account)
    assert {row["book_id"] for row in ledger} == {edition}
    assert {row["chapter_ordinal"] for row in ledger} == {1, 2}
    assert {row["purpose"] for row in ledger} == {"extraction", "extraction-review"}
    reservations, _ledger = notes_spend(database)
    assert {row["book_id"] for row in reservations} == {edition}
    [platform] = rows(database, "SELECT settled_usd,reserved_usd FROM platform_spend_days")
    assert platform["settled_usd"] == sum(row["usd"] for row in ledger)
    assert platform["reserved_usd"] == 0

    # A's chat is refused for A's own day, exactly as before; notes did not lift or use it.
    chat = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    assert a.ask(book) == [{"type": "error", "status": 429, "code": "allowance_reached",
                            "message": _DAY}]
    assert chat.writes == 0 and spend_of(database, a.owner) == ([], [])

    # A past every notes guard (and their cap lifted) chats as anyone: the guards are notes'.
    _seeded, seed_book = opened(memory_app, readers, title="Seeded", reading=False)
    seed_first_builds(database, a.owner, seed_book, 300, chapter=1)
    update_limits(database, a.owner, {"max_spend_usd_per_day": None})
    assert sentences(a.ask(book)) == [line(1, 2)]
    _reservations, own = spend_of(database, a.owner)
    assert own and not {row["purpose"] for row in own} & {"extraction", "extraction-review"}
    assert notes_spend(database)[1] == ledger


# ---- 5: the two spoiler properties of one ahead -----------------------------------------------------

# Every method of the worker's repository a story-memory run may call, and how far into the
# edition each can reach: the last chapter whose notes it reads ("notes"), or whose existence
# or kind it asks ("asks"), or which it writes ("writes"); None for a method that reads nothing
# of an edition's notes or chapters. A run calling any other method fails the test below, so a
# new way of reading the edition must be added here, with its bound, to pass.
REACH = {
    "read_edition": lambda edition, *, before: ("notes", before - 1),
    "next_chapter": lambda edition, *, through: ("asks", through),
    "chapter_published": lambda edition, ordinal: ("asks", ordinal),
    "unremembered_before": lambda edition, ordinal: ("asks", ordinal - 1),
    "begin_story_memory_chapter": lambda claim, edition, ordinal: ("writes", ordinal),
    "commit_chapter_memory": lambda claim, **publish: ("writes", publish["chapter"]["ordinal"]),
    "edition_of": lambda claim, **_create: None,
    "first_builds": lambda owner_id: None,
    "is_library_account": lambda owner_id: None,
    "heartbeat": lambda claim, **_lease: None,
    "start": lambda claim: None,
    "succeed": lambda claim: None,
    "fail": lambda claim, **_failure: None,
    "claim_next": lambda **_claim: None,
}


def recording(monkeypatch) -> list:
    """Record every public call on the worker's repository, with its reach (``REACH``); a call
    of a method not in ``REACH`` is recorded as ``("unknown", name)``."""
    reads = []
    for name in dir(PostgresWorkerRepository):
        if name.startswith("_") or not callable(getattr(PostgresWorkerRepository, name)):
            continue
        real = getattr(PostgresWorkerRepository, name)

        def recorded(self, *args, _name=name, _real=real, **kwargs):
            reach = REACH.get(_name)
            reads.append(("unknown", _name) if reach is None and _name not in REACH
                         else (_name, reach(*args, **kwargs) if reach else None))
            return _real(self, *args, **kwargs)
        monkeypatch.setattr(PostgresWorkerRepository, name, recorded)
    return reads


def test_a_run_for_a_reader_at_n_reads_notes_through_n_asks_of_n_plus_1_and_nothing_further(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The first spoiler property, restated for one ahead (ADR 0044 sections 3 and 4). Reader B
    has built the edition of the long lamp book to part 2; reader A has finished parts 1 and 2
    (n = 2) and read a page of part 3, so A's own run builds part 3. Then B builds on to part 9
    and A's next run finds everything up to its target built. The break it catches: A's runs
    reading the notes of part 3 or later (the working copy for part 3 is read below 3), asking
    of any chapter past part 3, writing anything but part 3, or calling any other method of the
    worker's repository (``REACH``)."""
    client, tokens, app = memory_app
    extraction = extraction_client()
    writer = Writer(extraction, monkeypatch)
    a = Reader(client, tokens, "a", readers["a"])
    b = Reader(client, tokens, "b", readers["b"])
    book_a = upload(client, tokens, "a", long_lamps())
    book_b = upload(client, tokens, "b", long_lamps())
    work = worker(app, extraction, worker_dsn)
    assert b.call("PUT", f"/api/books/{book_b}/read-page", tpage(1, 1, 2)).status_code == 200
    b.mark(book_b, [1])                                   # B's target: 2
    assert drain(work) == 2

    a.mark(book_a, [1, 2])
    assert a.call("PUT", f"/api/books/{book_a}/read-page", tpage(3, 1, 10)).status_code == 200
    reads = recording(monkeypatch)
    assert drain(work) == 1                               # A's run: part 3
    assert built_for(database, a.owner, book_a) == [(n, "body") for n in (1, 2, 3)]
    assert writer.parts() == [1, 2, 3]
    assert ("unknown",) not in {found[:1] for found in reads}, reads
    reached = [found[1] for found in reads if found[1] is not None]
    assert max(at for kind, at in reached if kind == "notes") == 2
    assert max(at for kind, at in reached) == 3
    assert ("writes", 3) in reached

    # B builds on to part 9; A's next run reads nothing of parts 4 to 9.
    monkeypatch.undo()
    writer = Writer(extraction, monkeypatch)
    b.mark(book_b, list(range(1, 9)))
    assert drain(work, 20) == 6
    assert built_for(database, b.owner, book_b) == [(n, "body") for n in range(1, 10)]
    calls = list(writer.calls)
    reads = recording(monkeypatch)
    StoryMemoryQueue(app.state.companion_dsn, OwnerId(a.owner)).ensure(book_a)
    assert drain(work) == 1
    assert ("unknown",) not in {found[:1] for found in reads}, reads
    reached = [found[1] for found in reads if found[1] is not None]
    assert reached and max(at for _kind, at in reached) <= 3
    assert not [found for found in reached if found[0] in ("notes", "writes")]
    assert writer.calls == calls
    assert handed(tenant_dsn, a.owner, book_a) == [1, 2]


def test_the_door_hands_a_reader_nothing_of_the_chapter_their_own_run_built_ahead(
        memory_app, readers, database, tenant_dsn, worker_dsn, monkeypatch):
    """The second spoiler property (ADR 0044 section 4: for chapter n+1 the door is the only
    protection). A has finished part 1, and A's own runs built parts 1 and 2; Corvus is first met
    in part 2. The break it catches: anything of part 2 handed to A by the raw door, the story
    screens (whatever bookmark is asked for), the chat's story memory, or anything the chat's
    writer and checker are given; or a reader who has only read a page (place 0) handed
    anything at all."""
    client, tokens, app = memory_app
    extraction = extraction_client()
    Lamps(extraction, monkeypatch)
    a, book = opened(memory_app, readers)
    a.mark(book, [1])                                     # place 1, target 2
    assert drain(worker(app, extraction, worker_dsn)) == 2
    edition = edition_of(database, a.owner, book)
    assert built_for(database, a.owner, book) == [(1, "body"), (2, "body")]
    assert "Corvus" in json.dumps(rows(database, "SELECT canonical_name FROM entities WHERE"
                                                 " owner_id=%s AND book_id=%s", *edition),
                                  default=str)

    for dsn in (tenant_dsn, database):
        [row] = door_rows(dsn, a.owner, book)
        assert sorted(item["ordinal"] for item in row["completed"]) == [1]
        assert "Corvus" not in json.dumps(row, default=str)
    for path in ("graph", "graph?bookmark=99", "notes", "notes?bookmark=99", "ingest"):
        shown = a.call("GET", f"/api/books/{book}/{path}")
        assert shown.status_code == 200 and "Corvus" not in shown.text, path
    corvus = rows(database, "SELECT id FROM entities WHERE owner_id=%s AND book_id=%s"
                            " AND canonical_name='Corvus'", *edition)[0]["id"]
    assert a.call("GET", f"/api/books/{book}/character/{corvus}").status_code == 404
    with HostedBookStore(tenant_dsn, OwnerId(a.owner)).book(book) as mem:
        assert [row["canonical_name"] for row in mem._audit_all("entities")] == ["Mara"]

    chat = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    assert sentences(a.ask(book, "Who has moved the lamps so far?")) == [line(1, 2)]
    assert chat.writes == 1 and "Corvus" not in chat.everything()

    # A reader at place 0 (one page read) of the same file: nothing, built or not.
    c, book_c = opened(memory_app, readers, "b")
    assert handed(tenant_dsn, c.owner, book_c) == []
    with HostedBookStore(tenant_dsn, OwnerId(c.owner)).book(book_c) as mem:
        assert mem._audit_all("entities") == []
