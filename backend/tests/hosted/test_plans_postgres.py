"""Gifts: the owner's own account and test accounts as paying readers without Paddle (ADR 0045
section 8, step 2), on real PostgreSQL.

A gift is written only by the operator's command (``python -m app.hosted.plans``), through
``set_gift``, which only the operator role may execute. A reader whose gift lasts today is
``paying``: the free ceiling does not apply to them and the reader's day and month caps (the
paying readers' hidden guards) do, with today's lines; an operator's own caps for that reader
still win. A gift that has passed changes nothing. A gifted reader's first question still stamps
their free weeks, once.

At the spend adapter, through the hosted HTTP app on the RLS-enforced tenant role, and through
the command on a role granted exactly what the deployment's role script grants the operator.
Stand-in writer and Jev only: no model is called and nothing is paid.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg import conninfo, sql

from app.hosted import plans
from app.hosted.companion.spend import _DAY, _FREE, _MONTH, set_spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_free_weeks_postgres as free_weeks
from tests.hosted.test_companion_spend_postgres import spend
from tests.hosted.test_companion_stream_postgres import (
    StandIns,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)
from tests.hosted.test_deploy import load_roles
from tests.hosted.test_free_weeks_postgres import (
    QUESTION,
    chat_since_start,
    chat_spent,
    move_start,
    opened,
    refused,
    shelve,
    stamp,
    started,
)

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
make_app = free_weeks.make_app
line = read_map_gate.line

SHOWN = {"owner_id", "state", "why", "paid_by", "gift_until", "free_started_at", "free_ends_at",
         "chat_usd_since_free_start"}


@pytest.fixture()
def operator_dsn(database: str, admin_dsn: str):
    """A login role holding exactly what ``scripts/hosted_roles.py`` grants the operator."""
    roles = load_roles()
    operator = next(role for role in roles.RUNTIME_ROLES if role.key == "operator")
    prefix = f"plans{uuid.uuid4().hex[:12]}"
    name = sql.Identifier(roles.role_name(prefix, operator))
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT BYPASSRLS").format(name))
    with psycopg.connect(database, autocommit=True) as conn:
        for statement in roles.grant_statements(prefix, operator):
            conn.execute(statement)
    try:
        yield conninfo.make_conninfo(database, user=roles.role_name(prefix, operator))
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(name))
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(name))


def command(dsn: str, monkeypatch, capsys, *argv: str) -> dict:
    """``python -m app.hosted.plans ...`` with DATABASE_URL set to ``dsn``: what it printed."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    assert plans.main(list(argv)) == 0
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1
    return json.loads(printed)


def gift(dsn: str, owner: uuid.UUID, until: str | None = "9999-12-31") -> None:
    """Give (or with None end) a gift, past every role, by the function the command calls."""
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT set_gift(%s, %s::date)", (owner, until)).fetchone()[0]


def gift_relative(dsn: str, owner: uuid.UUID, days: int) -> None:
    """A gift ending ``days`` after today's UTC date (negative: it has passed)."""
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT set_gift(%s, (now() AT TIME ZONE 'UTC')::date + %s)", (owner, days))


def plan_of(dsn: str, owner: uuid.UUID, usd: str = "2.10") -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state, why, free_started_at, free_ends_at, paid_by,"
                            " free_spent_usd FROM reader_plan_state(%s, 14, %s)",
                            (owner, Decimal(usd))).fetchone()


def row_of(dsn: str, owner: uuid.UUID):
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT to_jsonb(plan) FROM reader_plans AS plan WHERE owner_id=%s",
                             (owner,)).fetchone()
    return None if found is None else found[0]


def caps(day, month, free="0.05") -> None:
    set_spend_limits(reader_usd_per_day=Decimal(day), reader_usd_per_month=Decimal(month),
                     platform_usd_per_day=100, free_usd=Decimal(free))


# ---- the command ----------------------------------------------------------------------------------

def test_the_command_gives_a_gift_for_good_or_to_a_day_ends_it_and_shows_it(
        operator_dsn, database, readers, monkeypatch, capsys):
    """The break it catches: the owner unable to give his own account a gift, for good or to a
    day, or to end one, from the operator container's role; a gift that does not make the reader
    paying; another reader touched; an unknown id given a row; or ``show`` printing anything but
    the state, the dates and the chat spend."""
    a, b = readers["a"], readers["b"]
    shown = command(operator_dsn, monkeypatch, capsys, "show", str(a))
    assert shown == {"owner_id": str(a), "state": "new", "why": None, "paid_by": None,
                     "gift_until": None, "free_started_at": None, "free_ends_at": None,
                     "chat_usd_since_free_start": "0"}

    shown = command(operator_dsn, monkeypatch, capsys, "gift", str(a))
    assert (shown["state"], shown["why"], shown["paid_by"], shown["gift_until"]) == (
        "paying", None, "gift", "for good")
    assert row_of(database, a)["gift_until"] == "9999-12-31"
    assert set(shown) == SHOWN

    shown = command(operator_dsn, monkeypatch, capsys, "gift", str(a), "--until", "2099-06-30")
    assert (shown["state"], shown["paid_by"], shown["gift_until"]) == ("paying", "gift",
                                                                        "2099-06-30")

    shown = command(operator_dsn, monkeypatch, capsys, "gift", str(a), "--end")
    assert (shown["state"], shown["paid_by"], shown["gift_until"]) == ("new", None, None)
    assert row_of(database, a)["gift_until"] is None
    # Ending a gift nobody had makes no row.
    command(operator_dsn, monkeypatch, capsys, "gift", str(b), "--end")
    assert row_of(database, b) is None

    # An unknown account is refused and given nothing; so is a date with --end.
    stranger = uuid.uuid4()
    monkeypatch.setenv("DATABASE_URL", operator_dsn)
    for argv in (["gift", str(stranger)], ["show", str(stranger)],
                 ["gift", str(a), "--until", "2099-06-30", "--end"],
                 ["gift", str(a), "--until", "30/06/2099"]):
        with pytest.raises(SystemExit) as refused_with:
            plans.main(argv)
        assert refused_with.value.code == 2
    assert row_of(database, stranger) is None
    assert row_of(database, a)["gift_until"] is None


def test_show_prints_no_book_title_and_no_conversation_text(
        make_app, readers, database, operator_dsn, monkeypatch, capsys):
    """The break it catches: the owner's look at a reader's plan showing what they read or said:
    a book's title or author, a question, a reply, or anything beyond the state, the dates and
    the spend."""
    a, book, app = opened(make_app, readers)
    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    events = a.ask(book, QUESTION)
    assert events[-1]["type"] == "done" and sentences(events) == [line(1, 2)]
    gift(database, a.owner)

    monkeypatch.setenv("DATABASE_URL", operator_dsn)
    assert plans.main(["show", str(a.owner)]) == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)
    assert set(shown) == SHOWN and shown["state"] == "paying"
    assert shown["free_started_at"] is not None
    assert Decimal(shown["chat_usd_since_free_start"]) == chat_since_start(database, a.owner)
    for words in ("Lamp", "Ada Writer", QUESTION, line(1, 2), "lamp", "Mara", str(book)):
        assert words not in printed, words


# ---- who may give a gift --------------------------------------------------------------------------

def test_the_tenant_role_cannot_give_a_gift_or_write_one(tenant_dsn, database, readers):
    """The break it catches: a reader's session that could give itself (or another reader) a
    gift, by calling ``set_gift`` or by writing ``gift_until``."""
    a, b = readers["a"], readers["b"]
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        conn.execute("SELECT start_free_weeks()")          # the reader's own row exists
        for statement, params in (
                ("SELECT set_gift(%s, '9999-12-31'::date)", (a,)),
                ("SELECT set_gift(%s, '9999-12-31'::date)", (b,)),
                ("SELECT set_gift(%s, NULL::date)", (a,)),
                ("UPDATE reader_plans SET gift_until = '9999-12-31'", ()),
                ("INSERT INTO reader_plans (owner_id, gift_until) VALUES (%s, '9999-12-31')",
                 (b,))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
        assert conn.execute("SELECT state, paid_by FROM reader_plan_state(%s, 14, 2.10)",
                            (a,)).fetchone() == ("free", None)
    assert row_of(database, a)["gift_until"] is None and row_of(database, b) is None


# ---- a gifted reader is a paying reader -----------------------------------------------------------

def test_a_gifted_reader_is_not_stopped_by_the_free_ceiling_and_is_held_by_the_day_and_month(
        tenant_dsn, database, readers):
    """The break it catches: the owner or a tester stopped by the free ceiling; or a gifted
    reader held by nothing (the day and month guards of a paying reader skipped); or an
    operator's own caps for that reader ignored once they are gifted."""
    a = readers["a"]
    book = shelve(database, a)
    caps("0.10", "0.20")
    stamp(tenant_dsn, a)                                    # in the free weeks, and gifted
    gift(database, a)
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.06)                                 # past the $0.05 free ceiling
    spend(chat, book, 0.04)
    assert plan_of(database, a, "0.05")[:2] == ("paying", None)
    refused(chat, book, "day", _DAY)                        # today's day, today's line
    caps("1", "0.10")
    refused(chat, book, "month", _MONTH)
    caps("1", "5")
    spend(chat, book, 0.01)

    # An operator's own caps for that reader win, lowered or raised.
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("0.05")})
    refused(chat, book, "day", _DAY)
    caps("0.10", "0.20")
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("5"),
                                "max_spend_usd_per_month": Decimal("50")})
    spend(chat, book, 0.30)

    # The gift ended: the reader is back where they would be, here at the free ceiling.
    update_limits(database, a, {"max_spend_usd_per_day": None, "max_spend_usd_per_month": None})
    gift(database, a, None)
    refused(chat, book, "free", _FREE)


def test_a_gifted_reader_who_never_asked_is_paying_and_their_first_question_starts_the_free_weeks(
        make_app, readers, database, monkeypatch):
    """The break it catches: a gifted reader who has not asked yet shown as new; their first
    question not stamping the free weeks (they could have them again after the gift ends) or
    stamping them twice; or a gifted reader at today's day cap answered past it or told anything
    but today's line. Through the hosted app, with today's caps."""
    a, book, app = opened(make_app, readers, hosted_free_usd=0.05)
    models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    gift(database, a.owner)
    assert plan_of(database, a.owner)[:2] == ("paying", None) and started(database, a.owner) is None

    assert a.ask(book)[-1]["type"] == "done"
    first = started(database, a.owner)
    assert first is not None and plan_of(database, a.owner)[:2] == ("paying", None)
    move_start(database, a.owner, "1 day")
    first = started(database, a.owner)
    chat_spent(database, a.owner, Decimal("0.06"))          # past the free ceiling, under the day
    assert sentences(a.ask(book)) == [line(1, 2)]
    assert started(database, a.owner) == first

    chat_spent(database, a.owner, Decimal("0.10"))          # past today's $0.10 day
    writes = models.writes
    assert a.ask(book) == [{"type": "error", "status": 429, "code": "allowance_reached",
                            "message": _DAY}]
    assert models.writes == writes

    # The gift lasted past the two weeks; when it ends, the free weeks are over, not begun again.
    move_start(database, a.owner, "20 days")
    moved = started(database, a.owner)
    gift(database, a.owner, None)
    assert plan_of(database, a.owner)[:2] == ("ended", "time")
    a.ask(book)
    assert started(database, a.owner) == moved
    assert plan_of(database, a.owner)[:2] == ("ended", "time")


def test_a_gift_that_has_passed_changes_nothing(tenant_dsn, database, readers):
    """The break it catches: a gift that has passed still making the reader paying, or moving
    them anywhere but where they would be without it: new, in their free weeks (with the free
    ceiling), or ended. A gift ending today still holds today."""
    a, b = readers["a"], readers["b"]
    book = shelve(database, a)
    caps("0.10", "2")
    stamp(tenant_dsn, a)
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.05)
    before = {owner: plan_of(database, owner, "0.05") for owner in (a, b)}
    assert before[a][:2] == ("ended", "ceiling") and before[b][:2] == ("new", None)

    for owner in (a, b):
        gift_relative(database, owner, 0)                   # ends at the end of today: it holds
        assert plan_of(database, owner, "0.05")[:2] == ("paying", None)
        gift_relative(database, owner, -1)                  # ended yesterday
    assert {owner: plan_of(database, owner, "0.05") for owner in (a, b)} == before
    refused(chat, book, "free", _FREE)
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT gift_until FROM reader_plan_state(%s, 14, 0.05)",
                            (a,)).fetchone()[0] is not None
    move_start(database, a, "15 days")
    assert plan_of(database, a, "0.05")[:2] == ("ended", "time")


def test_a_gift_for_one_reader_never_touches_another(tenant_dsn, database, readers):
    """The break it catches: a gift given, changed or ended for one reader writing another's row
    or moving their state; or one reader's session able to see another's gift."""
    a, b = readers["a"], readers["b"]
    stamp(tenant_dsn, b)
    b_row, b_plan = row_of(database, b), plan_of(database, b)
    for until in ("9999-12-31", "2099-06-30", None):
        gift(database, a, until)
        assert (row_of(database, b), plan_of(database, b)) == (b_row, b_plan)
    gift(database, a)
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b),))
        assert conn.execute("SELECT owner_id FROM reader_plans").fetchall() == [(b,)]
        assert conn.execute("SELECT state, paid_by, gift_until FROM reader_plan_state"
                            "(%s, 14, 2.10)", (a,)).fetchone() == ("new", None, None)
        assert conn.execute("SELECT state, paid_by FROM reader_plan_state(%s, 14, 2.10)",
                            (b,)).fetchone() == ("free", None)
