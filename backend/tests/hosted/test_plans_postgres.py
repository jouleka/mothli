"""Gifts: the owner's own account and test accounts as paying readers without Paddle (ADR 0045
section 8, step 2; ADR 0046), on real PostgreSQL.

A gift is written only by the operator's command (``python -m app.hosted.plans``), through
``set_gift``, which only the operator role may execute. A reader whose gift lasts today is
``paying``: the reader's day and month caps (the paying readers' hidden guards) hold them, with
today's lines; an operator's own caps for that reader still win. A gift that has passed leaves
the reader ``ended`` with why ``plan``, and the gate answers; a reader who never had a plan or a
gift is ``new``, and the gate answers them too.

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
from app.hosted.companion.spend import _DAY, _MONTH, set_spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_mothli_gate_postgres as mothli_gate
from tests.hosted.test_companion_spend_postgres import spend
from tests.hosted.test_companion_stream_postgres import (
    StandIns,
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
    sentences,
)
from tests.hosted.test_deploy import load_roles
from tests.hosted.test_mothli_gate_postgres import (
    QUESTION,
    chat_spent,
    gate_of,
    gated,
    opened,
    refused,
    shelve,
)

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
# New readers: the gifts are what these tests give.
readers = read_map_gate.new_readers
make_app = mothli_gate.make_app
line = read_map_gate.line

SHOWN = {"owner_id", "state", "why", "paid_by", "gift_until"}


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


def plan_of(dsn: str, owner: uuid.UUID) -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state, why, paid_by FROM reader_plan_state(%s)",
                            (owner,)).fetchone()


def row_of(dsn: str, owner: uuid.UUID):
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT to_jsonb(plan) FROM reader_plans AS plan WHERE owner_id=%s",
                             (owner,)).fetchone()
    return None if found is None else found[0]


def caps(day, month) -> None:
    set_spend_limits(reader_usd_per_day=Decimal(day), reader_usd_per_month=Decimal(month),
                     platform_usd_per_day=100)


# ---- the command ----------------------------------------------------------------------------------

def test_the_command_gives_a_gift_for_good_or_to_a_day_ends_it_and_shows_it(
        operator_dsn, database, readers, monkeypatch, capsys):
    """The break it catches: the owner unable to give his own account a gift, for good or to a
    day, or to end one, from the operator container's role; a gift that does not make the reader
    paying; another reader touched; an unknown id given a row; or ``show`` printing anything but
    the state and the gift's date."""
    a, b = readers["a"], readers["b"]
    shown = command(operator_dsn, monkeypatch, capsys, "show", str(a))
    assert shown == {"owner_id": str(a), "state": "new", "why": None, "paid_by": None,
                     "gift_until": None}

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
    a book's title or author, a question, a reply, or anything beyond the state and the gift."""
    a, book, app = opened(make_app, readers)
    StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    gift(database, a.owner)
    events = a.ask(book, QUESTION)
    assert events[-1]["type"] == "done" and sentences(events) == [line(1, 2)]

    monkeypatch.setenv("DATABASE_URL", operator_dsn)
    assert plans.main(["show", str(a.owner)]) == 0
    printed = capsys.readouterr().out
    shown = json.loads(printed)
    assert set(shown) == SHOWN and shown["state"] == "paying"
    for words in ("Lamp", "Ada Writer", QUESTION, line(1, 2), "lamp", "Mara", str(book)):
        assert words not in printed, words


# ---- who may give a gift --------------------------------------------------------------------------

def test_the_tenant_role_cannot_give_a_gift_write_its_plan_or_see_another_readers(
        tenant_dsn, database, readers):
    """The break it catches: a reader's session that could give itself (or another reader) a
    gift or a plan, by calling ``set_gift``, writing ``gift_until``, inserting, moving or erasing
    its row; that finds a way left to start anything free (the removed ``start_free_weeks``); or
    that can read another reader's row or state."""
    a, b = readers["a"], readers["b"]
    gift_relative(database, a, -1)                          # the reader's own row exists: ended
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        for statement, params in (
                ("SELECT set_gift(%s, '9999-12-31'::date)", (a,)),
                ("SELECT set_gift(%s, '9999-12-31'::date)", (b,)),
                ("SELECT set_gift(%s, NULL::date)", (a,)),
                ("UPDATE reader_plans SET gift_until = '9999-12-31'", ()),
                ("INSERT INTO reader_plans (owner_id, gift_until) VALUES (%s, '9999-12-31')",
                 (b,)),
                ("DELETE FROM reader_plans", ())):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
        with pytest.raises(psycopg.errors.UndefinedFunction):
            conn.execute("SELECT start_free_weeks()")
        assert conn.execute("SELECT state, why, paid_by FROM reader_plan_state(%s)",
                            (a,)).fetchone() == ("ended", "plan", None)
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b),))
        assert conn.execute("SELECT count(*) FROM reader_plans").fetchone()[0] == 0
        assert conn.execute("SELECT state, why, gift_until FROM reader_plan_state(%s)",
                            (a,)).fetchone() == ("new", None, None)
    assert row_of(database, a)["gift_until"] is not None and row_of(database, b) is None


# ---- a gifted reader is a paying reader -----------------------------------------------------------

def test_a_gifted_reader_is_held_by_the_day_and_month_and_gated_once_the_gift_ends(
        tenant_dsn, database, readers):
    """The break it catches: a gifted reader held by nothing (the day and month guards of a
    paying reader skipped); an operator's own caps for that reader ignored once they are gifted;
    or a reader whose gift was taken back or has passed answered past the gate."""
    a = readers["a"]
    book = shelve(database, a)
    caps("0.10", "0.20")
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    gated(chat, book, None)                                 # new: the gate
    gift(database, a)
    spend(chat, book, 0.06)
    spend(chat, book, 0.04)
    assert plan_of(database, a) == ("paying", None, "gift")
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

    # The gift taken back: they never had a plan of their own, so they are new again, and the
    # gate answers, whatever their own caps. A gift that has passed: ended, and the gate answers.
    gift(database, a, None)
    assert plan_of(database, a) == ("new", None, None)
    gated(chat, book, None)
    gift_relative(database, a, -1)
    assert plan_of(database, a) == ("ended", "plan", None)
    gated(chat, book, "plan")


def test_a_gifted_reader_asks_through_the_app_and_meets_the_gate_when_the_gift_passes(
        make_app, readers, database, monkeypatch):
    """The break it catches: a gifted reader refused by the gate; a gifted reader at today's day
    cap answered past it or told anything but today's line; or a reader whose gift has passed
    answered. Through the hosted app, with low caps."""
    a, book, app = opened(make_app, readers)
    models = StandIns(app.state.platform_client, monkeypatch, reply=line(1, 2))
    gift(database, a.owner)
    assert plan_of(database, a.owner) == ("paying", None, "gift")
    assert sentences(a.ask(book)) == [line(1, 2)]

    chat_spent(database, a.owner, Decimal("0.10"))          # past today's $0.10 day
    writes = models.writes
    assert a.ask(book) == [{"type": "error", "status": 429, "code": "allowance_reached",
                            "message": _DAY}]
    assert models.writes == writes

    gift_relative(database, a.owner, -1)
    assert plan_of(database, a.owner)[:2] == ("ended", "plan")
    refused_now = a.stream(book)
    assert (refused_now.status_code, refused_now.json()) == (402, gate_of("plan"))
    assert models.writes == writes


def test_a_gift_holds_to_the_end_of_its_day_and_then_ends_the_plan(tenant_dsn, database, readers):
    """The break it catches: a gift ending today not holding today; a gift that has passed still
    making the reader paying, or leaving them new (as if they never had one) rather than ended."""
    a, b = readers["a"], readers["b"]
    book = shelve(database, a)
    caps("1", "5")
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    for owner in (a, b):
        gift_relative(database, owner, 0)                   # ends at the end of today: it holds
        assert plan_of(database, owner) == ("paying", None, "gift")
        gift_relative(database, owner, -1)                  # ended yesterday
        assert plan_of(database, owner) == ("ended", "plan", None)
    gated(chat, book, "plan")
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT gift_until FROM reader_plan_state(%s)",
                            (a,)).fetchone()[0] is not None


def test_a_gift_for_one_reader_never_touches_another(tenant_dsn, database, readers):
    """The break it catches: a gift given, changed or ended for one reader writing another's row
    or moving their state; or one reader's session able to see another's gift."""
    a, b = readers["a"], readers["b"]
    gift_relative(database, b, -1)
    b_row, b_plan = row_of(database, b), plan_of(database, b)
    for until in ("9999-12-31", "2099-06-30", None):
        gift(database, a, until)
        assert (row_of(database, b), plan_of(database, b)) == (b_row, b_plan)
    gift(database, a)
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b),))
        assert conn.execute("SELECT owner_id FROM reader_plans").fetchall() == [(b,)]
        assert conn.execute("SELECT state, paid_by, gift_until FROM reader_plan_state(%s)",
                            (a,)).fetchone() == ("new", None, None)
        assert conn.execute("SELECT state, paid_by FROM reader_plan_state(%s)",
                            (b,)).fetchone() == ("ended", None)
