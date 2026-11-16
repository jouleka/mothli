"""The platform pays: the companion's spend and caps in hosted mode (ADR 0041 part 3), on PostgreSQL.

Every model call goes through the unchanged ``budgeted_completion`` / ``budgeted_jev`` with
stand-in clients, over ``HostedCompanionCatalog``. Most tests run on the non-BYPASSRLS tenant
role, so row-level security and the adapter's own predicates are both on; the isolation tests take
each barrier away in turn. No real model is called and nothing is paid.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import conninfo, sql

from app.config import Settings
from app.cost import CostCeilingExceeded, budgeted_completion
from app.hosted.auth.repository import AuthConfigurationError
from app.hosted.companion.spend import (
    PlatformAllowanceReached,
    ReaderAllowanceReached,
    set_spend_limits,
    spend_limits,
)
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import inspect_limits, update_limits
from app.hosted.tenant.grants import TENANT_FUNCTION_PRIVILEGES, TENANT_TABLE_PRIVILEGES
from app.hosted.tenant.models import OwnerId
from app.hosted.tenant.repository import PostgresTenantRepository
from app.llm.jev import JevUnavailable
from app.llm.jev_budget import budgeted_jev
from tests.hosted import test_companion_read_map_postgres as read_map_gate

pytestmark = pytest.mark.postgres

# The tenant gate's database and RLS-enforced role (granted from grants.py), and two readers.
admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
reading_app = read_map_gate.reading_app

SETTINGS = Settings(_env_file=None)          # the per-call token ceilings the wrappers apply


@pytest.fixture(autouse=True)
def caps():
    """Generous process caps unless a test lowers them; restored afterwards."""
    before = spend_limits()
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=5, platform_usd_per_day=100)
    yield
    set_spend_limits(reader_usd_per_day=before.reader_usd_per_day,
                     reader_usd_per_month=before.reader_usd_per_month,
                     platform_usd_per_day=before.platform_usd_per_day)


def shelve(dsn: str, owner: uuid.UUID) -> str:
    """One live book of ``owner``, written past row-level security."""
    book = uuid.uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO books (owner_id,id,incarnation,title,author,schema_version)"
            " VALUES (%s,%s,%s,'A book','An author',1)", (owner, book, uuid.uuid4()))
    return str(book)


def catalog(dsn: str, owner: uuid.UUID) -> HostedCompanionCatalog:
    return HostedCompanionCatalog(dsn, OwnerId(owner))


def cap(dsn: str, owner: uuid.UUID, **limits) -> None:
    update_limits(dsn, owner, limits)


def spend(adapter, book, usd, phase="stream-reply", model="gpt-4o-mini"):
    """One settled companion call of exactly ``usd``, as the wrappers record it."""
    held = adapter.reserve_cost(book, phase=phase, model=model, input_tokens=10, output_tokens=10,
                                usd=usd, max_input_tokens=1, max_output_tokens=1, max_usd=0.0)
    adapter.settle_cost(book, held)


def reserve(adapter, book, usd, phase="stream-sentence-check", model="jev-1.14.0"):
    return adapter.reserve_cost(book, phase=phase, model=model, input_tokens=10, output_tokens=10,
                                usd=usd, max_input_tokens=1, max_output_tokens=1, max_usd=0.0)


class Writer:
    """A stand-in writer priced like gpt-4o-mini; counts the calls that reach the provider."""

    provider = "openai-compatible"
    _is_native_openai = True

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls, self.fail = 0, fail

    def billing_model(self, tier):
        return "gpt-4o-mini"

    _model_for = billing_model

    def complete(self, system, user, *, tier, schema=None, max_output_tokens=None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return "Mara kept lamp 2.", {"in": 40, "out": 12}


def write(adapter, book, writer):
    return budgeted_completion(adapter, SETTINGS, writer, book, phase="stream-reply",
                               system="Reply.", user="What did Mara do?", tier="large")


def jev(fail: Exception | None = None):
    calls = []

    def evaluate(state, questions, **_options):
        calls.append(state)
        if fail is not None:
            raise fail
        return {"q": {"noul": .9}}, {"in": 50, "out": 5}

    return SimpleNamespace(model="jev-1.14.0", evaluate=evaluate), calls


def check(adapter, book, checker):
    return budgeted_jev(adapter, SETTINGS, checker, book, phase="stream-sentence-check",
                        state={"sentence": "Mara kept lamp 2."}, questions={"q": {"type": "noul"}})


def refusal(raised) -> Exception:
    """The allowance under the wrappers' CostCeilingExceeded."""
    assert isinstance(raised.value, CostCeilingExceeded)
    return raised.value.__cause__


def ledger(dsn: str, owner: uuid.UUID) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT book_id,phase,purpose,model,input_tokens,output_tokens,usd FROM cost_ledger"
            " WHERE owner_id=%s ORDER BY recorded_at,id", (owner,)).fetchall()


def reservations(dsn: str, owner: uuid.UUID) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT id,state,purpose,reserved_usd FROM cost_reservations WHERE owner_id=%s"
            " ORDER BY created_at,id", (owner,)).fetchall()


def platform_today(dsn: str) -> tuple:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT settled_usd,reserved_usd FROM platform_spend_days"
            " WHERE day=(now() AT TIME ZONE 'UTC')::date").fetchone()
    return row or (Decimal(0), Decimal(0))


def test_each_cap_refuses_the_call_that_would_pass_it_before_the_provider_is_called(
    tenant_dsn: str, database: str, readers: dict
) -> None:
    """The break it catches: a call that passes a reader's day, month or lifetime dollars, their
    calls in flight, or the platform's day, and is still made (and paid); or one reader at a cap
    holding up another. Each refusal must come before the writer is reached and leave nothing
    reserved."""
    a, b = readers["a"], readers["b"]
    book_a, book_b = shelve(database, a), shelve(database, b)
    owner_a, owner_b = catalog(tenant_dsn, a), catalog(tenant_dsn, b)

    def refused(adapter, book, limit, code="allowance_reached"):
        writer, before = Writer(), reservations(database, adapter_owner[adapter])
        platform = platform_today(database)
        with pytest.raises(CostCeilingExceeded) as raised:
            write(adapter, book, writer)
        cause = refusal(raised)
        assert (cause.limit, cause.code) == (limit, code)
        assert writer.calls == 0
        assert reservations(database, adapter_owner[adapter]) == before
        assert platform_today(database) == platform
        return cause

    def passes(adapter, book):
        writer = Writer()
        write(adapter, book, writer)
        assert writer.calls == 1

    adapter_owner = {owner_a: a, owner_b: b}

    # The day: A's own cap, $0.10; B keeps the platform default ($1 here) and is not held up.
    cap(database, a, max_spend_usd_per_day=Decimal("0.10"))
    spend(owner_a, book_a, 0.099)
    assert isinstance(refused(owner_a, book_a, "day"), ReaderAllowanceReached)
    passes(owner_b, book_b)
    # Spend before today's UTC midnight is not today's.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE cost_ledger SET recorded_at=recorded_at - interval '1 day'"
                     " WHERE owner_id=%s", (a,))
    passes(owner_a, book_a)

    # The month: last month's spend is not this month's; this month's counts whatever the day.
    cap(database, a, max_spend_usd_per_day=Decimal("1"), max_spend_usd_per_month=Decimal("0.20"))
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE cost_ledger SET recorded_at=date_trunc('month', now()) - interval '1 hour'"
            " WHERE owner_id=%s", (a,))
    passes(owner_a, book_a)
    spend(owner_a, book_a, 0.199)
    refused(owner_a, book_a, "month")
    passes(owner_b, book_b)

    # The lifetime cap an operator may set (LIT-47) still applies to companion calls.
    cap(database, a, max_spend_usd_per_month=Decimal("5"), max_spend_usd=Decimal("0.50"))
    with psycopg.connect(database) as conn:
        lifetime = conn.execute("SELECT sum(usd) FROM cost_ledger WHERE owner_id=%s",
                                (a,)).fetchone()[0]
    cap(database, a, max_spend_usd=lifetime + Decimal("0.001"))
    refused(owner_a, book_a, "lifetime")
    cap(database, a, max_spend_usd=None)

    # Calls in flight: two open companion calls of A's, and a third waits; B is not affected.
    cap(database, a, max_companion_calls_in_flight=2)
    first, _second = reserve(owner_a, book_a, 0.001), reserve(owner_a, "home", 0.001)
    busy = refused(owner_a, book_a, "calls_in_flight", code="companion_busy")
    assert "moment" in busy.reader_message
    passes(owner_b, book_b)
    owner_a.discard_cost_reservation(book_a, first)
    passes(owner_a, book_a)

    # The platform's day stops everyone, whoever spent it.
    with psycopg.connect(database) as conn:
        settled, held = conn.execute(
            "SELECT settled_usd,reserved_usd FROM platform_spend_days"
            " WHERE day=(now() AT TIME ZONE 'UTC')::date").fetchone()
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=5,
                     platform_usd_per_day=settled + held + Decimal("0.001"))
    for adapter, book in ((owner_b, book_b), (owner_a, book_a)):
        assert isinstance(refused(adapter, book, "platform_day"), PlatformAllowanceReached)


def test_reservations_racing_for_the_last_cent_admit_exactly_what_fits(
    tenant_dsn: str, database: str, readers: dict
) -> None:
    """The break it catches: two requests at once both reading the same remaining allowance and
    both reserving, so a cap is passed. For one reader the owner lock serializes them; across
    readers, the platform day's row lock does."""
    a, b = readers["a"], readers["b"]
    book_a, book_b = shelve(database, a), shelve(database, b)

    def race(attempts):
        start, results = threading.Barrier(len(attempts)), []

        def attempt(adapter, book, usd):
            start.wait()
            try:
                results.append(reserve(adapter, book, usd))
            except (ReaderAllowanceReached, PlatformAllowanceReached) as exc:
                results.append(exc)

        threads = [threading.Thread(target=attempt, args=item) for item in attempts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return [r for r in results if isinstance(r, str)], [r for r in results if not isinstance(r, str)]

    # One reader, $0.10 a day, $0.09 spent: two calls of $0.01 race for the last cent.
    cap(database, a, max_spend_usd_per_day=Decimal("0.10"))
    spend(catalog(tenant_dsn, a), book_a, 0.09)
    won, lost = race([(catalog(tenant_dsn, a), book_a, 0.01) for _ in range(2)])
    assert len(won) == 1 and [exc.limit for exc in lost] == ["day"]
    # Eight more at once from a fresh day's room: exactly what fits.
    cap(database, a, max_spend_usd_per_day=Decimal("0.19"))
    won, lost = race([(catalog(tenant_dsn, a), book_a, 0.03) for _ in range(8)])
    assert len(won) == 3 and len(lost) == 5

    # Two readers against the platform's day: $0.06 left there, eight calls of $0.02 from both.
    cap(database, a, max_spend_usd_per_day=Decimal("1"))
    settled, held = platform_today(database)
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=5,
                     platform_usd_per_day=settled + held + Decimal("0.06"))
    won, lost = race([(catalog(tenant_dsn, owner), book, 0.02)
                      for owner, book in ((a, book_a), (b, book_b)) * 4])
    assert len(won) == 3 and all(isinstance(exc, PlatformAllowanceReached) for exc in lost)
    settled_after, held_after = platform_today(database)
    assert settled_after + held_after == settled + held + Decimal("0.06")


def test_a_refused_provider_call_releases_its_reservation_and_a_timed_out_one_stays_counted(
    tenant_dsn: str, database: str, readers: dict
) -> None:
    """The break it catches: charging a reader (and the platform) for a call the provider turned
    away before doing any work, or forgetting a call whose outcome is unknown, so retries after
    timeouts could spend without bound. Same wrappers, same rule as local mode (audit 6u)."""
    a = readers["a"]
    book = shelve(database, a)
    adapter = catalog(tenant_dsn, a)
    before = platform_today(database)

    refused_checker, calls = jev(JevUnavailable("no credits", reason="no_credits"))
    with pytest.raises(JevUnavailable):
        check(adapter, book, refused_checker)
    assert len(calls) == 1
    assert ledger(database, a) == []
    assert [row[1] for row in reservations(database, a)] == ["released"]
    assert adapter.get_cost_reservations(book) == [] and adapter.get_costs(book) == []
    assert platform_today(database) == before

    lost_checker, _ = jev(JevUnavailable("timed out"))
    with pytest.raises(JevUnavailable):
        check(adapter, book, lost_checker)
    with pytest.raises(TimeoutError):
        write(adapter, book, Writer(fail=TimeoutError("writer timed out")))
    costs = adapter.get_costs(book)
    assert [cost["phase"] for cost in costs] == ["stream-sentence-check-failed-reserved",
                                                 "stream-reply-failed-reserved"]
    assert all(cost["usd"] > 0 for cost in costs)
    assert [row[1] for row in reservations(database, a)] == ["released", "settled", "settled"]
    settled, held = platform_today(database)
    assert held == before[1] and settled == before[0] + sum(row[6] for row in ledger(database, a))

    # Counted means counted: with the day cap at what is already charged, the next call waits.
    cap(database, a, max_spend_usd_per_day=sum(row[6] for row in ledger(database, a)))
    with pytest.raises(CostCeilingExceeded):
        check(adapter, book, jev()[0])


def test_a_reservation_left_open_by_a_crash_still_counts(
    tenant_dsn: str, database: str, readers: dict
) -> None:
    """The break it catches: a reservation forgotten when the process that made it dies before
    settling (no settle, no release), which would let the next process spend past the cap."""
    a = readers["a"]
    book = shelve(database, a)
    reserve(catalog(tenant_dsn, a), book, 0.08)            # ... and the process dies here
    cap(database, a, max_spend_usd_per_day=Decimal("0.10"), max_companion_calls_in_flight=2)

    restarted = catalog(tenant_dsn, a)
    assert [r["reserved_usd"] for r in restarted.get_cost_reservations(book)] == [0.08]
    with pytest.raises(ReaderAllowanceReached) as raised:
        reserve(restarted, book, 0.03)
    assert raised.value.limit == "day"
    reserve(restarted, book, 0.02)
    with pytest.raises(ReaderAllowanceReached) as raised:
        reserve(restarted, book, 0)
    assert raised.value.limit == "calls_in_flight"
    assert platform_today(database)[1] >= Decimal("0.10")


def test_one_readers_spend_is_hidden_from_another_by_row_level_security_alone(
    tenant_dsn: str, database: str, readers: dict
) -> None:
    """The break it catches: spend tables without a working owner policy, so a query that forgot
    its owner predicate would show, settle, release or plant another reader's spend; and a
    platform total the tenant role can read (with two readers, the total less one's own is the
    other's). Here no predicate names the owner: only the policy and the grants do."""
    a, b = readers["a"], readers["b"]
    book_a = shelve(database, a)
    adapter = catalog(tenant_dsn, a)
    spend(adapter, book_a, 0.01)
    open_id = reserve(adapter, book_a, 0.01)
    reserve(adapter, "home", 0.01)
    before = (ledger(database, a), reservations(database, a))

    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(b),))
        assert conn.execute("SELECT count(*) FROM cost_ledger").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM cost_reservations").fetchone()[0] == 0
        assert conn.execute("UPDATE cost_reservations SET state='released'").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                """INSERT INTO cost_ledger (owner_id,phase,model,usd,idempotency_key,purpose)
                   VALUES (%s,'synthesis','x',1,'planted','stream-reply')""", (a,))
        for statement in ("SELECT * FROM platform_spend_days",
                          "UPDATE platform_spend_days SET reserved_usd=0",
                          "INSERT INTO platform_spend_days (day) VALUES ('2000-01-01')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
    assert (ledger(database, a), reservations(database, a)) == before

    # The two functions are the only way to the total, and neither returns it.
    with psycopg.connect(database) as conn:
        results = dict(conn.execute(
            """SELECT proname, pg_get_function_result(oid) FROM pg_proc
               WHERE proname IN ('companion_platform_reserve','companion_platform_adjust')"""
        ).fetchall())
        columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema='public' AND table_name='platform_spend_days'")}
    assert results == {"companion_platform_reserve": "boolean",
                       "companion_platform_adjust": "void"}
    assert columns == {"day", "settled_usd", "reserved_usd", "updated_at"}
    assert open_id


def test_spend_adapter_isolates_owners_by_its_own_predicates_with_rls_bypassed(
    database: str, readers: dict
) -> None:
    """The break it catches: a spend query that leans on row-level security alone. Through a
    superuser connection, an adapter bound to reader B must not see A's ledger or reservations,
    must not settle, note or release A's reservations even knowing their ids, and must not spend
    against A's book; A's caps and B's are counted apart."""
    a, b = readers["a"], readers["b"]
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]
    book_a, book_b = shelve(database, a), shelve(database, b)
    owner_a, other = catalog(database, a), catalog(database, b)
    spend(owner_a, book_a, 0.05)
    held_book = reserve(owner_a, book_a, 0.01)
    held_home = reserve(owner_a, "home", 0.01)
    assert len(owner_a.get_costs(book_a)) == 1 and len(owner_a.get_cost_reservations("home")) == 1
    before = (ledger(database, a), reservations(database, a))

    assert other.get_costs(book_a) == [] and other.get_cost_reservations(book_a) == []
    assert other.get_cost_reservations("home") == [] and other.get_costs("home") == []
    for book, held in ((book_a, held_book), ("home", held_home), (book_b, held_book)):
        with pytest.raises(ValueError, match="unknown cost reservation"):
            other.settle_cost(book, held)
        with pytest.raises(ValueError, match="unknown cost reservation"):
            other.note_reservation_actual(book, held, input_tokens=0, output_tokens=0, usd=0.0)
        with pytest.raises(ValueError, match="unknown cost reservation"):
            other.discard_cost_reservation(book, held)
    with pytest.raises(ValueError, match="unknown book"):
        reserve(other, book_a, 0.01)
    assert (ledger(database, a), reservations(database, a)) == before

    # A at a day cap of exactly what A has spent and holds; B, with nothing spent, is unaffected.
    cap(database, a, max_spend_usd_per_day=Decimal("0.07"))
    cap(database, b, max_spend_usd_per_day=Decimal("0.07"))
    with pytest.raises(ReaderAllowanceReached):
        reserve(owner_a, book_a, 0.001)
    other.settle_cost(book_b, reserve(other, book_b, 0.07))
    assert ledger(database, b)[0][6] == Decimal("0.07")


def test_the_tenant_role_holds_exactly_the_spend_grants(tenant_dsn: str, database: str) -> None:
    """The break it catches: a tenant role that can read or write the platform total directly, a
    spend function anyone may call, or a missing grant that would refuse every reply. Startup
    refuses a role with any privilege on the platform total."""
    assert TENANT_TABLE_PRIVILEGES["cost_ledger"] == {"SELECT", "INSERT"}
    assert TENANT_TABLE_PRIVILEGES["cost_reservations"] == {"SELECT", "INSERT", "UPDATE"}
    assert "platform_spend_days" not in TENANT_TABLE_PRIVILEGES
    repository = PostgresTenantRepository(tenant_dsn)
    asyncio.run(repository.check_runtime_role())
    role = conninfo.conninfo_to_dict(tenant_dsn)["user"]
    stranger = f"spend_stranger_{uuid.uuid4().hex}"
    with psycopg.connect(database, autocommit=True) as conn:
        for function in TENANT_FUNCTION_PRIVILEGES:
            assert conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                (role, function)).fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(stranger)))
        try:
            for function in TENANT_FUNCTION_PRIVILEGES:
                assert not conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                                        (stranger, function)).fetchone()[0]
        finally:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(stranger)))
        conn.execute(sql.SQL("GRANT SELECT ON platform_spend_days TO {}").format(
            sql.Identifier(role)))
    try:
        with pytest.raises(AuthConfigurationError, match="allow-list"):
            asyncio.run(repository.check_runtime_role())
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("REVOKE SELECT ON platform_spend_days FROM {}").format(
                sql.Identifier(role)))
    asyncio.run(repository.check_runtime_role())


def test_reading_keeps_working_at_the_cap(reading_app, readers: dict, database: str) -> None:
    """The break it catches: a reader at their allowance (or the platform at its day's) losing
    reading itself: the shelf, the read map and the place take no AI and must keep answering."""
    client, tokens, app = reading_app
    a = readers["a"]
    book = read_map_gate.upload(client, tokens, "a", read_map_gate.lamp_book())
    cap(database, a, max_spend_usd_per_day=Decimal("0"))
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=5, platform_usd_per_day=0)
    with pytest.raises(CostCeilingExceeded):
        check(catalog(app.state.companion_dsn, a), book, jev()[0])
    csrf = read_map_gate._become(client, tokens, "a")
    assert client.get("/api/books").status_code == 200
    assert client.put(f"/api/books/{book}/read-chapters", json={"atoms": [1], "read": True},
                      headers={read_map_gate.CSRF_HEADER: csrf}).status_code == 200
    read = client.get(f"/api/books/{book}/read-map")
    assert read.status_code == 200 and read.json()["chapters"]
    assert client.get(f"/api/books/{book}/conversation").status_code == 200


def test_operator_tooling_shows_and_sets_the_new_caps(database: str, readers: dict) -> None:
    """The break it catches: an operator unable to set a reader's own day or month cap or calls in
    flight, or ``limits show`` failing on the new dollar columns."""
    import json

    a = readers["a"]
    updated = update_limits(database, a, {"max_spend_usd_per_day": Decimal("0.25"),
                                          "max_spend_usd_per_month": Decimal("3"),
                                          "max_companion_calls_in_flight": 4})
    assert updated["max_spend_usd_per_day"] == "0.2500000000"
    assert updated["max_spend_usd_per_month"] == "3.0000000000"
    assert updated["max_companion_calls_in_flight"] == 4
    shown = inspect_limits(database, a)
    json.dumps(shown)
    cleared = update_limits(database, a, {"max_spend_usd_per_day": None})
    assert cleared["max_spend_usd_per_day"] is None
    with pytest.raises(psycopg.errors.CheckViolation):
        update_limits(database, a, {"max_companion_calls_in_flight": 0})


def test_spend_names_its_purpose_and_never_a_key(tenant_dsn: str, database: str,
                                                 readers: dict) -> None:
    """The break it catches: companion rows that cannot be told apart by feature (purpose), a
    Jev call recorded as the writer's, or a reservation id in a form the local wrappers cannot pass
    back."""
    a = readers["a"]
    book = shelve(database, a)
    adapter = catalog(tenant_dsn, a)
    write(adapter, book, Writer())
    check(adapter, book, jev()[0])
    rows = ledger(database, a)
    assert [(row[1], row[2]) for row in rows] == [("synthesis", "stream-reply"),
                                                  ("judge", "stream-sentence-check")]
    held = reserve(adapter, book, 0.001)
    assert len(held) == 32 and int(held, 16) >= 0
    with pytest.raises(ValueError):
        reserve(adapter, book, 0.001, phase="Not A Purpose!")
    adapter.settle_cost(book, held, phase="stream-reply-provider-limit-violation")
    assert ledger(database, a)[-1][2] == "stream-reply-provider-limit-violation"
    now = datetime.now(UTC)
    assert all(abs(datetime.fromisoformat(cost["at"]) - now) < timedelta(minutes=5)
               for cost in adapter.get_costs(book))
