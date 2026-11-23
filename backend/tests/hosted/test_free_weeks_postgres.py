"""A reader's two free weeks with Mothli (ADR 0045 step 1), on real PostgreSQL.

They start at the reader's first question, in a book or on the home screen, stamped once by the
database's clock. While they last the reader's day and month allowance is not applied, so the
$0.10 day no longer stops a new reader at about their sixth question; one ceiling in dollars of
their own chat spend since the start is applied instead, and a reply that began under it is
finished. After the two weeks, and for every reader not in them, the caps are exactly as before.
The plan only admits or refuses a model call: what is read, searched and sent is the same.

Through the hosted HTTP app on the RLS-enforced tenant role, and at the spend adapter. Stand-in
writer and Jev only: no model is called and nothing is paid.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import stream_conversation
from app.cost import CostCeilingExceeded
from app.hosted.companion.spend import (
    _DAY,
    _FREE,
    _LIFETIME,
    _MONTH,
    PlatformAllowanceReached,
    ReaderAllowanceReached,
    SpendStore,
    set_spend_limits,
)
from app.hosted.companion.store import HostedCompanionCatalog, ReplyCatalog
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from app.main import create_app
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted.test_companion_spend_postgres import Background, Writer, reserve, spend, write
from tests.hosted.test_companion_stream_postgres import (
    CHAT,
    ORDINARY,
    Reader,
    StandIns,
    events_of,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)
from tests.hosted.test_tenant_postgres import UnusedOIDCClient

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
lamp_book, line, upload = read_map_gate.lamp_book, read_map_gate.line, read_map_gate.upload

# Today's caps on main: $0.10 a reader's UTC day, $2.00 their UTC month.
TODAY = {**CHAT, "hosted_reader_usd_per_day": 0.10, "hosted_reader_usd_per_month": 2.00}
# About what one reply costs (ADR 0045, Context): six of them pass the $0.10 day.
REPLY = Decimal("0.021")
QUESTION = "What did Mara do?"


@pytest.fixture()
def make_app(tenant_dsn: str, readers: dict, tmp_path):
    """The hosted app with today's caps and any settings given; one per test."""
    with ExitStack() as stack:
        def make(**overrides):
            tokens, sessions = read_map_gate.signed_in(readers)
            app = create_app(
                read_map_gate.hosted_settings(tenant_dsn, tmp_path / "objects",
                                              **{**TODAY, **overrides}),
                auth_repository=sessions, oidc_client=UnusedOIDCClient(),
                auth_clock=lambda: datetime(2026, 7, 16, 12, tzinfo=UTC))
            client = stack.enter_context(TestClient(app, base_url="https://reader.example"))
            return client, tokens, app

        yield make


def started(dsn: str, owner: uuid.UUID):
    """When this reader's free weeks started, or None: read past row-level security."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT free_started_at FROM reader_plans WHERE owner_id=%s",
                           (owner,)).fetchone()
    return None if row is None else row[0]


def state(dsn: str, owner: uuid.UUID, days: int = 14, usd: str = "2.10") -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state, why FROM reader_plan_state(%s, %s, %s)",
                            (owner, days, Decimal(usd))).fetchone()


def move_start(dsn: str, owner: uuid.UUID, ago: str) -> None:
    """Put this reader's start ``ago`` (an interval) before now, as the days passing would."""
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE reader_plans SET free_started_at = now() - %s::interval"
                     " WHERE owner_id=%s", (ago, owner))


def chat_spent(dsn: str, owner: uuid.UUID, usd: Decimal, *, background: bool = False) -> None:
    """One settled companion row of ``usd`` today: the reader's own chat, or Mothli's background
    work for them, written past row-level security as the worker or a reply would leave it."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO cost_ledger (owner_id, phase, model, usd, idempotency_key, purpose,"
            " background) VALUES (%s, 'synthesis', 'gpt-6.1-terra', %s, %s, %s, %s)",
            (owner, usd, f"companion:{uuid.uuid4().hex}",
             "notes-write" if background else "stream-reply", background))


def chat_today(dsn: str, owner: uuid.UUID) -> Decimal:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT COALESCE(sum(usd), 0) FROM cost_ledger WHERE owner_id=%s AND NOT background"
            " AND recorded_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
            (owner,)).fetchone()[0]


def chat_since_start(dsn: str, owner: uuid.UUID) -> Decimal:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT COALESCE(sum(usd), 0) FROM cost_ledger AS entry JOIN reader_plans AS plan"
            " USING (owner_id) WHERE owner_id=%s AND NOT entry.background"
            " AND entry.recorded_at >= plan.free_started_at", (owner,)).fetchone()[0]


def opened(make_app, readers, label="a", **settings):
    """A reader with the lamp book, part 1 read: ready to ask, and not yet in the free weeks."""
    client, tokens, app = make_app(**settings)
    someone = Reader(client, tokens, label, readers[label])
    book = upload(client, tokens, label, lamp_book())
    someone.mark(book, [1])
    return someone, book, app


def stamp(tenant: str, owner: uuid.UUID) -> None:
    """The reader's first question, at the adapter: what the ask routes call first."""
    HostedCompanionCatalog(tenant, OwnerId(owner)).start_free_weeks()


def refused(adapter, book, limit: str, message: str) -> None:
    with pytest.raises(ReaderAllowanceReached) as raised:
        reserve(adapter, book, 0.001)
    assert (raised.value.limit, raised.value.reader_message) == (limit, message)


def shelve(dsn: str, owner: uuid.UUID) -> str:
    book = uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO books (owner_id,id,incarnation,title,author,schema_version)"
                     " VALUES (%s,%s,%s,'A book','An author',1)", (owner, book, uuid.uuid4()))
    return str(book)


# ---- the sixth question ---------------------------------------------------------------------------

def test_a_reader_in_the_free_weeks_is_not_stopped_by_the_day_and_day_15_is_today(
        make_app, readers, database, monkeypatch):
    """The break it catches: the problem ADR 0045 removes. With today's $0.10 day a new reader is
    stopped at about their sixth question. In the free weeks they are answered well past it (ten
    replies' worth of spend, then eight more questions); and the same reader, the same spend, on
    day 15, sees today's line again."""
    a, book, app = opened(make_app, readers)
    models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    assert started(database, a.owner) is None             # uploading and reading start nothing
    assert state(database, a.owner) == ("new", None)
    events = a.ask(book)
    assert events[-1]["type"] == "done" and sentences(events) == [line(1, 2)]
    first = started(database, a.owner)
    assert first is not None and state(database, a.owner) == ("free", None)

    chat_spent(database, a.owner, REPLY * 10)               # ten replies: past the $0.10 day
    for _ in range(8):
        events = a.ask(book)
        assert events[-1]["type"] == "done", events[-1]
        assert sentences(events) == [line(1, 2)]
    assert chat_today(database, a.owner) > 2 * Decimal("0.10")
    assert models.writes == 9 and started(database, a.owner) == first

    # Day 15: the free weeks are over, and today's caps are back, with today's line.
    move_start(database, a.owner, "14 days 1 minute")
    assert state(database, a.owner) == ("ended", "time")
    writes = models.writes
    assert a.ask(book) == [{"type": "error", "status": 429, "code": "allowance_reached",
                            "message": _DAY}]
    assert models.writes == writes
    # A minute before the end of day 14 they were still in them.
    move_start(database, a.owner, "13 days 23 hours 59 minutes")
    assert sentences(a.ask(book)) == [line(1, 2)]


def test_after_the_free_weeks_the_day_and_the_month_are_todays(tenant_dsn, database, readers):
    """The break it catches: a reader past their free weeks kept free of the day or the month
    (the free weeks would never end), or held by the free ceiling instead of today's caps."""
    a = readers["a"]
    book = shelve(database, a)
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=Decimal("0.20"),
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    stamp(tenant_dsn, a)
    move_start(database, a, "15 days")
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    chat_spent(database, a, Decimal("0.10"))
    refused(chat, book, "day", _DAY)
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=Decimal("0.10"),
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    refused(chat, book, "month", _MONTH)
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=5,
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    spend(chat, book, 0.01)                                # past the free ceiling: not applied


# ---- the free ceiling -----------------------------------------------------------------------------

def test_the_free_ceiling_finishes_the_reply_under_way_and_stops_the_next(
        make_app, readers, database, monkeypatch):
    """The break it catches: a reply cut off part-way because the ceiling was met while it was
    being written; the next reply let through past the ceiling; or the reader shown a number, a
    count, a countdown or "unlimited", or anything but the one line of ADR 0045 section 6."""
    a, book, app = opened(make_app, readers, hosted_free_usd=0.30)
    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    assert a.ask(book)[-1]["type"] == "done"                 # the first question starts them
    chat_spent(database, a.owner, Decimal("0.30") - chat_since_start(database, a.owner)
               - Decimal("0.0000001"))                       # a hair under the ceiling
    assert state(database, a.owner, usd="0.30") == ("free", None)

    real, past = SpendStore.reserve_cost, []

    def reserve_cost(adapter, book_id, **call):
        spent = chat_since_start(database, a.owner)
        held = real(adapter, book_id, **call)
        past.append(spent >= Decimal("0.30"))
        return held

    monkeypatch.setattr(SpendStore, "reserve_cost", reserve_cost)
    models = StandIns(app.state.platform_client, monkeypatch, reply=ORDINARY)
    events = a.ask(book)
    assert events[-1]["type"] == "done", events[-1]
    assert sentences(events) == [line(1, 2), "She seems careful with it.", line(1, 3),
                                 "It looks like she trusts Jonas less now.",
                                 "What did you make of that?"]
    assert past[0] is False and any(past)                  # admitted under it, finished past it
    assert state(database, a.owner, usd="0.30") == ("ended", "ceiling")

    writes, judged = models.writes, models.judged
    events = a.ask(book)
    assert events == [{"type": "error", "status": 429, "code": "allowance_reached",
                       "message": _FREE}]
    assert (models.writes, models.judged) == (writes, judged)
    assert _FREE == ("We’ve talked so much these first days that your free start with me is all "
                     "used up. That’s a good sign! Your books, your place and your highlights "
                     "stay right here, free, always.")
    assert not any(character.isdigit() for character in _FREE)
    assert "unlimited" not in _FREE.casefold() and "question" not in _FREE.casefold()
    # Reading takes no AI and goes on.
    a.read(book, 2, 1, 5)
    # Asking on the home screen meets the same ceiling, with the same line.
    response = a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
    assert events_of(response) == events
    # Once the two weeks are over, today's caps apply (here the $0.10 day, long passed).
    move_start(database, a.owner, "15 days")
    assert a.ask(book)[-1]["message"] == _DAY


def test_background_work_never_counts_toward_the_free_ceiling_nor_is_stopped_by_it(
        tenant_dsn, database, readers):
    """The break it catches: Mothli's background work for a reader (their chapter notes, what it
    remembers of them) used up the free start the reader can see, or held back by it."""
    a = readers["a"]
    book = shelve(database, a)
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    stamp(tenant_dsn, a)
    chat, work = HostedCompanionCatalog(tenant_dsn, OwnerId(a)), Background(tenant_dsn, OwnerId(a))
    spend(work, book, 0.40, phase="notes-write")           # eight times the free ceiling
    chat_spent(database, a, Decimal("0.40"), background=True)
    assert state(database, a, usd="0.05") == ("free", None)
    spend(chat, book, 0.049)
    reserve(chat, book, 0.001)
    chat_spent(database, a, Decimal("0.001"))
    refused(chat, book, "free", _FREE)
    spend(work, book, 0.01, phase="notes-write")           # background work goes on meanwhile


# ---- the clock ------------------------------------------------------------------------------------

def test_the_clock_starts_at_the_first_question_once_and_nothing_else_moves_it(
        make_app, readers, database, monkeypatch):
    """The break it catches: the free weeks started by the app's own fetches (the cast, the story
    so far, the conversation), by reading, or not at all by a question on the home screen; or
    started again by a second question, a removed book, a cleared conversation or a new book."""
    a, book, app = opened(make_app, readers)
    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    a.read(book, 2, 1, 5)
    a.become()
    for path in (f"/api/books/{book}/graph", f"/api/books/{book}/notes",
                 f"/api/books/{book}/ingest", f"/api/books/{book}/conversation",
                 f"/api/books/{book}/read-map", "/api/reader/conversation",
                 "/api/reader/profile", "/api/books"):
        a.client.get(path)
    assert started(database, a.owner) is None

    assert a.ask(book)[-1]["type"] == "done"
    move_start(database, a.owner, "3 days")                # so a second stamp would show
    first = started(database, a.owner)
    a.ask(book)
    a.call("POST", "/api/reader/ask/stream", {"question": "Who is Mara?"})
    assert a.call("DELETE", f"/api/books/{book}/conversation").status_code == 204
    assert a.call("DELETE", "/api/reader/conversation").status_code == 204
    assert a.call("DELETE", f"/api/books/{book}").status_code == 204
    again = upload(a.client, a.tokens, "a", lamp_book())
    a.mark(again, [1])
    a.ask(again)
    assert started(database, a.owner) == first

    # A question on the home screen starts them too.
    b = Reader(a.client, a.tokens, "b", readers["b"])
    assert started(database, b.owner) is None
    b.call("POST", "/api/reader/ask/stream", {"question": "What should I read next?"})
    assert started(database, b.owner) is not None


def test_the_tenant_role_cannot_write_its_plan_choose_a_date_or_see_another_readers(
        tenant_dsn, database, readers):
    """The break it catches: a reader's session that could start, move or erase its own free weeks
    (or another's) by writing the row, pass a date or an account to the start, or read another
    reader's row or state."""
    a, b = readers["a"], readers["b"]
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT start_free_weeks()")          # no reader set: nobody is stamped
        assert started(database, a) is None and started(database, b) is None
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        conn.execute("SELECT start_free_weeks()")
        assert conn.execute("SELECT count(*) FROM reader_plans").fetchone()[0] == 1
        for statement, params in (
                ("INSERT INTO reader_plans (owner_id, free_started_at) VALUES (%s, now())", (b,)),
                ("UPDATE reader_plans SET free_started_at = now() - interval '30 days'", ()),
                ("DELETE FROM reader_plans", ())):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
        for call, params in (("SELECT start_free_weeks(now() + interval '30 days')", ()),
                             ("SELECT start_free_weeks(%s::uuid)", (b,)),
                             ("SELECT start_free_weeks(%s::uuid, now())", (b,))):
            with pytest.raises(psycopg.errors.UndefinedFunction):
                conn.execute(call, params)
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b),))
        assert conn.execute("SELECT count(*) FROM reader_plans").fetchone()[0] == 0
        assert conn.execute("SELECT state, why, free_started_at FROM reader_plan_state"
                            "(%s, 14, 2.10)", (a,)).fetchone() == ("new", None, None)
    assert started(database, a) is not None and started(database, b) is None


def test_two_readers_free_weeks_never_touch(tenant_dsn, database, readers):
    """The break it catches: one reader's question starting another's free weeks, one reader's
    spend counted toward another's ceiling, or one reader at the ceiling holding up another."""
    a, b = readers["a"], readers["b"]
    book_a, book_b = shelve(database, a), shelve(database, b)
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    stamp(tenant_dsn, a)
    assert started(database, b) is None
    chat_a = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    chat_b = HostedCompanionCatalog(tenant_dsn, OwnerId(b))
    spend(chat_a, book_a, 0.05)
    refused(chat_a, book_a, "free", _FREE)
    assert started(database, b) is None
    spend(chat_b, book_b, 0.001)                           # A at the ceiling holds nobody else
    stamp(tenant_dsn, b)
    assert state(database, b, usd="0.05") == ("free", None)
    spend(chat_b, book_b, 0.04)                            # A's spend is not counted for B
    move_start(database, a, "15 days")
    assert state(database, b, usd="0.05") == ("free", None)
    assert state(database, a, usd="0.05") == ("ended", "time")
    assert started(database, a) != started(database, b)


# ---- an operator's own caps for a reader ----------------------------------------------------------

def test_an_operators_own_caps_for_a_reader_win_over_the_free_weeks(tenant_dsn, database, readers):
    """How an ``owner_limits`` override combines with the free weeks (ADR 0045 step 1): a reader
    whose row sets their own day or month cap is held by those caps exactly as before, in the
    free weeks or not, and the free ceiling is not applied to them. The free weeks replace only
    the defaults. The break it catches: an operator who lowered one reader's day (a reader making
    Mothli spend without bound) ignored for two weeks, or one who raised it for a tester or for
    themselves stopped by the ceiling. The lifetime cap, the calls in flight and the platform's
    day hold a reader in the free weeks as they hold everyone."""
    a, b = readers["a"], readers["b"]
    book_a, book_b = shelve(database, a), shelve(database, b)
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=100, free_usd=Decimal("0.20"))
    for owner in (a, b):
        stamp(tenant_dsn, owner)
    chat_a = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    chat_b = HostedCompanionCatalog(tenant_dsn, OwnerId(b))

    # Lowered: A's own day of $0.05 holds, with today's line.
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("0.05")})
    spend(chat_a, book_a, 0.05)
    refused(chat_a, book_a, "day", _DAY)
    # Raised: B's own day and month are far above the ceiling, and B goes past it.
    update_limits(database, b, {"max_spend_usd_per_day": Decimal("5"),
                                "max_spend_usd_per_month": Decimal("50")})
    spend(chat_b, book_b, 0.30)
    spend(chat_b, book_b, 0.01)
    assert state(database, b, usd="0.20") == ("ended", "ceiling")
    # With their own caps cleared, B is back under the free ceiling, which they passed.
    update_limits(database, b, {"max_spend_usd_per_day": None, "max_spend_usd_per_month": None})
    refused(chat_b, book_b, "free", _FREE)

    # The rest holds in the free weeks too: lifetime, calls in flight, the platform's day.
    update_limits(database, a, {"max_spend_usd_per_day": None})
    with psycopg.connect(database) as conn:
        conn.execute("DELETE FROM cost_ledger WHERE owner_id=%s", (a,))
    chat_spent(database, a, Decimal("0.01"))
    update_limits(database, a, {"max_spend_usd": Decimal("0.0105")})
    refused(chat_a, book_a, "lifetime", _LIFETIME)
    update_limits(database, a, {"max_spend_usd": None, "max_companion_calls_in_flight": 1})
    held = reserve(chat_a, book_a, 0.001)
    with pytest.raises(ReaderAllowanceReached) as raised:
        reserve(chat_a, book_a, 0.001)
    assert raised.value.limit == "calls_in_flight"
    chat_a.discard_cost_reservation(book_a, held)
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=0, free_usd=Decimal("0.20"))
    with pytest.raises(PlatformAllowanceReached):
        reserve(chat_a, book_a, 0.001)


def test_a_reply_admitted_in_the_free_weeks_is_not_cut_off_by_the_ceiling_at_the_adapter(
        tenant_dsn, database, readers):
    """The break it catches, at the adapter: a reply's later calls refused for the free ceiling
    once its first call was admitted under it; or that admission carried to the next reply."""
    a = readers["a"]
    book = shelve(database, a)
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    stamp(tenant_dsn, a)
    spend(HostedCompanionCatalog(tenant_dsn, OwnerId(a)), book, 0.049)
    reply = ReplyCatalog(tenant_dsn, OwnerId(a))
    spend(reply, book, 0.02)                               # admitted under; the ceiling passed
    spend(reply, book, 0.03)
    reserve(reply, book, 0.01)
    writer = Writer()
    with pytest.raises(CostCeilingExceeded) as raised:
        write(ReplyCatalog(tenant_dsn, OwnerId(a)), book, writer)
    assert raised.value.__cause__.limit == "free" and writer.calls == 0


# ---- what is read, searched and sent ----------------------------------------------------------------

def test_the_free_weeks_change_nothing_a_reply_reads_searches_or_sends(
        make_app, readers, database, monkeypatch):
    """Spoiler safety (ADR 0045 section 9): the plan check only admits or refuses a model call.
    The break it catches: anything a reply reads, searches or sends that differs because the
    reader is in their free weeks, or gifted (ADR 0045 step 2), or paying through Paddle (step
    3). The same reader, book, place and question, in the free weeks, on day 15, with a gift and
    on a Paddle plan: the passages retrieved, the writer's prompt and system, and everything Jev
    is given are identical."""
    a, book, app = opened(make_app, readers)
    a.read(book, 2, 1, 5)
    retrieved = []
    real = stream_conversation.compact_candidates

    def compact_candidates(*args, **options):
        found = real(*args, **options)
        retrieved.append(json.dumps(found, sort_keys=True, default=str))
        return found

    monkeypatch.setattr(stream_conversation, "compact_candidates", compact_candidates)

    def asked():
        retrieved.clear()
        models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
        events = a.ask(book, QUESTION)
        assert events[-1]["type"] == "done" and sentences(events) == [line(1, 2)]
        assert a.call("DELETE", f"/api/books/{book}/conversation").status_code == 204
        return (list(retrieved), models.systems, models.prompts,
                [json.dumps(checked, sort_keys=True) for checked in models.checked])

    in_the_free_weeks = asked()
    assert state(database, a.owner) == ("free", None)
    move_start(database, a.owner, "15 days")
    assert state(database, a.owner) == ("ended", "time")
    after_them = asked()
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (a.owner,))
    assert state(database, a.owner) == ("paying", None)
    gifted = asked()
    # ADR 0045 step 3: paying through Paddle, as a checked message leaves the row.
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, NULL::date)", (a.owner,))
        conn.execute("UPDATE reader_plans SET paddle_customer_id = %s,"
                     " paddle_subscription_id = %s, paddle_status = 'active',"
                     " paddle_occurred_at = now() WHERE owner_id = %s",
                     ("ctm_" + "c" * 26, "sub_" + "s" * 26, a.owner))
        assert conn.execute("SELECT state, paid_by FROM reader_plan_state(%s, 14, 2.10)",
                            (a.owner,)).fetchone() == ("paying", "paddle")
    paying = asked()
    assert in_the_free_weeks[0] and in_the_free_weeks[2] and in_the_free_weeks[3]
    assert after_them == in_the_free_weeks
    assert gifted == in_the_free_weeks
    assert paying == in_the_free_weeks
    assert "Corvus moved lamp 6" not in "".join(in_the_free_weeks[2])
