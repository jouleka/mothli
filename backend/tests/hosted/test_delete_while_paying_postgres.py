"""Deleting an account while paying (ADR 0045 step 7), on real PostgreSQL.

A reader who asks to delete their account while their Paddle plan would renew is never charged
again: the cancel at the end of the period paid for is recorded as owed before the account is
marked, asked of Paddle through the code "Cancel my plan" runs, and asked again by the regular
check until Paddle's answer brings the scheduled end, whatever Paddle did at the request. Coming
back within the 30 days leaves the plan ending; "Keep my plan" renews it. The purge never erases
an account whose plan still renews. The copy of one's data holds "your plan.json".

Through the hosted HTTP app, signed in with the CSRF token, the billing role and the purge role
granted exactly what the deployment grants them. Paddle is the stand-in of
``test_paddle_subscribe_postgres`` behind the real client, holding the owner's other product's
subscription and customer too: nothing calls Paddle or any other outside service, and no model
is called. The sign-in service's deletion and keep are stood in, writing ``users`` as the real
repository does.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest

from app.hosted.auth.api import CSRF_HEADER
from app.hosted.paddle_api import PaddleApi
from app.hosted.paddle_check import check
from app.hosted.purge import PostgresAccountPurge, owner_tables, purge_due_accounts
from app.hosted.tenant.grants import purge_grants
from tests.hosted import test_paddle_messages_postgres as messages
from tests.hosted.test_account_deletion_postgres import _role
from tests.hosted.test_companion_stream_postgres import (
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
)
from tests.hosted.test_billing import OPERATOR
from tests.hosted.test_paddle_messages_postgres import PADDLE, message, paddle_id, post, sub_id
from tests.hosted.test_paddle_subscribe_postgres import (
    CHECKOUT_URL,
    KEY,
    MYSIGNER_CUSTOMER,
    MYSIGNER_SUB,
    PRICES,
    StandInPaddle,
    allowed,
    ask,
    plan,
    row,
    subscribe,
)
from tests.hosted.test_tenant_postgres import _become

pytestmark = pytest.mark.postgres

admin_dsn = messages.admin_dsn
database = messages.database
tenant_dsn = messages.tenant_dsn
make_app = messages.make_app
billing_dsn = messages.billing_dsn
CANCEL = {"effective_from": "next_billing_period"}
HELD = "purge held an account whose Paddle plan still renews"
FAILURES = ("timeout", "down", "busy", ("refuse", 400, "bad_request"),
            ("refuse", 409, "subscription_locked_renewal"))


@pytest.fixture()
def readers(database: str) -> dict:
    """Five readers, so each way of standing has one of its own."""
    owners = {label: uuid.uuid4() for label in "abcde"}
    with psycopg.connect(database) as conn:
        for label, owner in owners.items():
            conn.execute("INSERT INTO users (id, display_name) VALUES (%s, %s)",
                         (owner, f"Reader {label.upper()}"))
    return owners


def _deletable(app, dsn: str) -> None:
    """The sign-in service's deletion and keep, as ``PostgresAuthRepository`` does them to
    ``users``; the reader's sessions then wait to be erased, as signing in again would show."""
    sessions = app.state.auth_repository

    def mark(owner, erase_after) -> None:
        for digest, principal in list(sessions.sessions.items()):
            if principal.owner_id == owner:
                sessions.sessions[digest] = dataclasses.replace(principal,
                                                                erase_after=erase_after)

    async def request_deletion(*, owner_id, now):
        with psycopg.connect(dsn) as conn:
            found = conn.execute(
                "UPDATE users SET status = 'deleting', erase_after = now() + interval '32 days',"
                " updated_at = now() WHERE id = %s AND status = 'active' AND deleted_at IS NULL"
                " RETURNING erase_after", (owner_id,)).fetchone()
        if found is None:
            return None
        mark(owner_id, found[0])
        return found[0]

    async def restore_account(*, owner_id):
        with psycopg.connect(dsn) as conn:
            found = conn.execute(
                "UPDATE users SET status = 'active', erase_after = NULL, updated_at = now()"
                " WHERE id = %s AND status = 'deleting' AND deleted_at IS NULL RETURNING id",
                (owner_id,)).fetchone()
        if found is not None:
            mark(owner_id, None)
        return found is not None

    sessions.request_deletion = request_deletion
    sessions.restore_account = restore_account


@pytest.fixture()
def desk(make_app, billing_dsn, database):
    """The hosted app with Paddle's messages and API on, Paddle stood in, and accounts that can
    be deleted and kept: (client, tokens, stand-in, app)."""
    client, tokens, app = make_app(**PADDLE, paddle_api_key=KEY, paddle_checkout_url=CHECKOUT_URL,
                                   hosted_billing_dsn=billing_dsn, **OPERATOR)
    standin = StandInPaddle()
    app.state.paddle_api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    client.checkouts_dsn = database
    _deletable(app, database)
    return client, tokens, standin, app


@pytest.fixture()
def purge_dsn(admin_dsn: str, database: str):
    with psycopg.connect(database) as conn:
        tables = purge_grants(owner_tables(conn))
    with _role(admin_dsn, database, bypass=True, tables=tables) as dsn:
        yield dsn


def account(client, tokens, label: str, route: str):
    csrf = _become(client, tokens, label)
    return client.post(f"/api/account/{route}", headers={CSRF_HEADER: csrf})


def delete(client, tokens, label: str):
    response = account(client, tokens, label, "delete")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"erase_after"}
    return response


def ending(client, tokens, label: str) -> dict:
    _become(client, tokens, label)
    response = client.get("/api/billing/ending")
    assert response.status_code == 200, response.text
    return response.json()


def regular_check(billing_dsn: str, standin: StandInPaddle) -> tuple[int, int, int]:
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    return check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None)


def owed(dsn: str, owner) -> bool:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT cancel_owed_since IS NOT NULL FROM reader_plans"
                            " WHERE owner_id = %s", (owner,)).fetchone()[0]


def cancels(standin: StandInPaddle) -> list[str]:
    """The subscriptions Paddle was asked to cancel, in order."""
    return [path.split("/")[2] for method, path, _b, _h in standin.requests
            if method == "POST" and path.endswith("/cancel")]


def renews_at_paddle(standin: StandInPaddle, sub: str) -> bool:
    return standin.subscriptions[sub]["scheduled_change"] is None


def period_end(standin: StandInPaddle, sub: str) -> datetime:
    return datetime.fromisoformat(
        standin.subscriptions[sub]["current_billing_period"]["ends_at"].replace("Z", "+00:00"))


def due_now(dsn: str, owner) -> None:
    """The 30 days have passed."""
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE users SET erase_after = now() - interval '1 minute' WHERE id = %s",
                     (owner,))


def rows_of(dsn: str, owner) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        return {table: conn.execute(f"SELECT count(*) FROM {table} WHERE owner_id = %s",
                                    (owner,)).fetchone()[0]
                for table in ("reader_plans", "paddle_events", "paddle_checkouts")}


# ---- asking to delete -----------------------------------------------------------------------------

def test_asking_to_delete_schedules_the_cancel_at_the_period_end_for_that_reader_alone(
        desk, database, readers):
    """The break it catches: a paying reader who asks to delete their account charged again (no
    cancel asked of Paddle, or one that ends the plan at once instead of at the end of what they
    paid for); a cancel sent for another reader's subscription or the owner's other product; any
    other reader's plan changed; the waiting screen not told the plan won't renew and its day."""
    client, tokens, standin, _app = desk
    a, b = readers["a"], readers["b"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    subscribe(client, tokens, standin, "b", b, sub_id("b"), which="yearly")
    b_row, b_plan = row(database, b), plan(client, tokens, "b")
    mysigner = json.loads(json.dumps(standin.subscriptions[MYSIGNER_SUB]))
    standin.requests.clear()

    delete(client, tokens, "a")

    assert [(m, p, body) for m, p, body, _h in standin.requests] == [
        ("POST", f"/subscriptions/{sub_id('a')}/cancel", CANCEL)]
    assert not renews_at_paddle(standin, sub_id("a"))
    kept = row(database, a)
    assert datetime.fromisoformat(kept["ends_at"]) == period_end(standin, sub_id("a"))
    assert kept["cancel_owed_since"] is not None and kept["paddle_status"] == "active"
    # Paying to the end of what they paid for, as any cancelled plan.
    assert messages.plan_of(database, a) == ("paying", None, "paddle")
    # The waiting screen: won't renew, and the day.
    seen = ending(client, tokens, "a")
    assert seen["wont_renew"] is True
    assert datetime.fromisoformat(seen["ends"]) == period_end(standin, sub_id("a"))
    # B, and the owner's other product, untouched.
    assert row(database, b) == b_row and plan(client, tokens, "b") == b_plan
    assert ending(client, tokens, "b") == {"wont_renew": False, "ends": None}
    assert standin.subscriptions[MYSIGNER_SUB] == mysigner
    stored = {sub_id("a"): kept["paddle_customer_id"], sub_id("b"): b_row["paddle_customer_id"]}
    allowed(standin, stored)

    # B deleting cancels B's alone.
    standin.requests.clear()
    delete(client, tokens, "b")
    assert cancels(standin) == [sub_id("b")]
    assert len(standin.requests) == 1
    assert standin.subscriptions[MYSIGNER_SUB] == mysigner


def test_a_reader_with_no_plan_that_renews_costs_no_call(desk, database, readers):
    """The break it catches: Paddle called (and the reader's deletion slowed or put at risk) for
    a reader with a gift only, one whose gift has passed, a new one, one whose plan ended, or one
    whose plan is already cancelled to the period's end; or any of them told their plan won't
    renew."""
    client, tokens, standin, _app = desk
    gifted, lapsed, new, ended, cancelled = (readers[label] for label in "abcde")
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (gifted,))
        conn.execute("SELECT set_gift(%s, '2020-01-31'::date)", (lapsed,))
    subscribe(client, tokens, standin, "d", ended, sub_id("d"))
    post(client, message(sub_id("d"), "canceled", customer=row(database, ended)[
        "paddle_customer_id"], custom={}, at=datetime.now(UTC) + timedelta(hours=1)), seed=False)
    assert messages.plan_of(database, ended) == ("ended", "plan", None)
    subscribe(client, tokens, standin, "e", cancelled, sub_id("e"))
    assert ask(client, tokens, "e", "cancel").json()["cancel_scheduled"] is True
    standin.requests.clear()

    for label in "abcd":
        delete(client, tokens, label)
        assert ending(client, tokens, label) == {"wont_renew": False, "ends": None}, label
    delete(client, tokens, "e")
    assert standin.requests == []
    # Already cancelled: it won't renew, and says so.
    assert ending(client, tokens, "e")["wont_renew"] is True
    assert messages.plan_of(database, gifted) == ("paying", None, "gift")


# ---- Paddle away or refusing at the request -------------------------------------------------------

@pytest.mark.parametrize("fail", FAILURES, ids=lambda fail: str(fail))
def test_paddle_away_or_refusing_never_stops_the_deletion_and_the_check_cancels_later(
        desk, database, billing_dsn, readers, fail, caplog):
    """The break it catches: a deletion refused, failed or lost because Paddle was slow, down,
    overloaded or refusing (inside the 30 minutes before a renewal, too); the cancel then
    forgotten, so the reader is charged at the renewal; the cancel sent twice in one request; the
    regular check not asking again, asking while Paddle is still away, asking for another
    reader, or asking again once Paddle has confirmed; a log line holding an id."""
    client, tokens, standin, _app = desk
    a, b = readers["a"], readers["b"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    caplog.set_level(logging.DEBUG)
    standin.requests.clear()
    standin.fail = fail

    delete(client, tokens, "a")

    assert cancels(standin) == [sub_id("a")] and len(standin.requests) == 1
    assert renews_at_paddle(standin, sub_id("a"))
    assert row(database, a)["ends_at"] is None and owed(database, a)
    # The waiting screen still says so: the cancel is owed, and the check makes it.
    seen = ending(client, tokens, "a")
    assert seen["wont_renew"] is True
    assert datetime.fromisoformat(seen["ends"]) == period_end(standin, sub_id("a"))
    if fail == FAILURES[-1]:
        assert "refund" in caplog.text

    # The regular check while Paddle is still away or refusing: nothing written.
    standin.requests.clear()
    before = row(database, a)
    regular_check(billing_dsn, standin)
    assert renews_at_paddle(standin, sub_id("a"))
    assert {key: value for key, value in row(database, a).items()
            if key not in ("paddle_occurred_at", "paddle_price_id")} == {
        key: value for key, value in before.items()
        if key not in ("paddle_occurred_at", "paddle_price_id")}

    # Paddle back: the owed plan first, then cancelled; B's only fetched.
    standin.fail = None
    standin.requests.clear()
    regular_check(billing_dsn, standin)
    assert standin.requests[0][:2] == ("GET", f"/subscriptions/{sub_id('a')}")
    assert cancels(standin) == [sub_id("a")]
    assert standin.requests[1][:3] == ("POST", f"/subscriptions/{sub_id('a')}/cancel", CANCEL)
    assert not renews_at_paddle(standin, sub_id("a")) and renews_at_paddle(standin, sub_id("b"))
    assert datetime.fromisoformat(row(database, a)["ends_at"]) == period_end(standin,
                                                                             sub_id("a"))
    # Confirmed: never asked again.
    standin.requests.clear()
    regular_check(billing_dsn, standin)
    assert cancels(standin) == []
    allowed(standin, {sub_id("a"): row(database, a)["paddle_customer_id"],
                      sub_id("b"): row(database, b)["paddle_customer_id"]})
    for secret in (sub_id("a"), sub_id("b"), str(a), KEY, "stand-in"):
        assert secret not in caplog.text, secret


# ---- changing one's mind ---------------------------------------------------------------------------

def test_coming_back_before_the_retry_leaves_the_plan_ending_and_keep_my_plan_renews_it(
        desk, database, billing_dsn, readers):
    """Paddle down at the request, the reader back before the check. The break it catches:
    coming back quietly undoing the cancel they were promised (charged at the renewal); "Keep my
    plan" not removing the owed cancel, so the check cancels a plan the reader chose to keep."""
    client, tokens, standin, _app = desk
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    standin.fail = "down"
    delete(client, tokens, "a")
    standin.fail = None
    assert account(client, tokens, "a", "keep").status_code == 204
    # Back: an ordinary account, the plan still owed its cancel; the check makes it.
    regular_check(billing_dsn, standin)
    assert not renews_at_paddle(standin, sub_id("a"))
    seen = plan(client, tokens, "a")
    assert (seen["subscribed"], seen["cancel_scheduled"]) == (True, True)
    # "Keep my plan", as the account sheet's button: renews, and nothing is owed any more.
    kept = ask(client, tokens, "a", "keep")
    assert kept.status_code == 200 and kept.json()["cancel_scheduled"] is False
    assert renews_at_paddle(standin, sub_id("a")) and not owed(database, a)
    standin.requests.clear()
    regular_check(billing_dsn, standin)
    assert cancels(standin) == [] and renews_at_paddle(standin, sub_id("a"))


def test_keep_my_plan_from_the_waiting_screen_before_the_retry_asks_paddle_nothing(
        desk, database, billing_dsn, readers):
    """Paddle down at the request; the reader comes back with "Keep my plan" (keeping the account,
    then the plan) before the check. The break it catches: the check later cancelling the plan
    the reader chose to keep; or Paddle asked to undo a cancel it never made."""
    client, tokens, standin, _app = desk
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    standin.fail = "down"
    delete(client, tokens, "a")
    standin.fail = None
    standin.requests.clear()
    assert account(client, tokens, "a", "keep").status_code == 204
    kept = ask(client, tokens, "a", "keep")
    assert kept.status_code == 200 and kept.json()["cancel_scheduled"] is False
    assert standin.requests == [] and not owed(database, a)
    regular_check(billing_dsn, standin)
    assert cancels(standin) == [] and renews_at_paddle(standin, sub_id("a"))


def test_coming_back_after_the_cancel_leaves_it_and_keep_my_plan_removes_it(
        desk, database, billing_dsn, readers):
    """The break it catches: coming back within the 30 days renewing the plan by itself; "Keep
    my plan" not removing the scheduled cancel at Paddle, or not for this reader alone."""
    client, tokens, standin, _app = desk
    a, b = readers["a"], readers["b"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    delete(client, tokens, "a")
    delete(client, tokens, "b")
    assert account(client, tokens, "a", "keep").status_code == 204
    seen = plan(client, tokens, "a")
    assert (seen["state"], seen["cancel_scheduled"]) == ("paying", True)
    regular_check(billing_dsn, standin)
    assert not renews_at_paddle(standin, sub_id("a"))
    standin.requests.clear()
    kept = ask(client, tokens, "a", "keep")
    assert kept.json()["cancel_scheduled"] is False
    assert [(m, p, body) for m, p, body, _h in standin.requests] == [
        ("PATCH", f"/subscriptions/{sub_id('a')}", {"scheduled_change": None})]
    assert renews_at_paddle(standin, sub_id("a")) and not renews_at_paddle(standin, sub_id("b"))
    assert owed(database, b)
    # B, still waiting to be erased, cannot keep A's plan or their own through the account
    # sheet's route: that needs an ordinary account.
    assert ask(client, tokens, "b", "keep").status_code == 403


def test_a_plan_a_late_payment_brings_to_an_account_waiting_to_be_erased_is_cancelled(
        desk, database, billing_dsn, readers):
    """A reader who opened a checkout, asked to delete their account, then paid from Paddle's
    reminder email. The break it catches: that new plan renewing on an account that will be
    erased (a reader left paying with no account)."""
    client, tokens, standin, _app = desk
    a = readers["a"]
    response = ask(client, tokens, "a", "checkout", {"plan": "monthly"})
    custom = standin.transactions[response.json()["transaction_id"]]["custom_data"]
    delete(client, tokens, "a")
    assert standin.requests[1:] == []                 # no plan: no call
    customer = paddle_id("ctm_", "latepayer")
    standin.subscriptions[sub_id("a")] = standin.entity(sub_id("a"), customer, custom=custom)
    payload = message(sub_id("a"), customer=customer, custom=custom,
                      event_type="subscription.created", at=datetime.now(UTC))
    assert post(client, payload, seed=False).status_code == 200
    assert messages.plan_of(database, a) == ("paying", None, "paddle")
    regular_check(billing_dsn, standin)
    assert cancels(standin) == [sub_id("a")] and not renews_at_paddle(standin, sub_id("a"))


def test_a_new_checkout_after_coming_back_is_not_owed_a_cancel(desk, database, billing_dsn,
                                                                readers):
    """The break it catches: a reader who deleted, came back, let the old plan end and then
    subscribed again having their new plan cancelled by the old request."""
    client, tokens, standin, _app = desk
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    delete(client, tokens, "a")
    assert account(client, tokens, "a", "keep").status_code == 204
    post(client, message(sub_id("a"), "canceled", customer=row(database, a)[
        "paddle_customer_id"], custom={}, at=datetime.now(UTC) + timedelta(hours=1)), seed=False)
    assert messages.plan_of(database, a) == ("ended", "plan", None)
    # The old plan's last message long past, so the new plan's first message is the newer one.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reader_plans SET paddle_occurred_at = now() - interval '1 day'"
                     " WHERE owner_id = %s", (a,))
    second = paddle_id("sub_", "secondplan")
    subscribe(client, tokens, standin, "a", a, second)
    assert row(database, a)["paddle_subscription_id"] == second
    assert messages.plan_of(database, a) == ("paying", None, "paddle")
    assert not owed(database, a)
    standin.requests.clear()
    regular_check(billing_dsn, standin)
    assert cancels(standin) == [] and renews_at_paddle(standin, second)


# ---- erasure -------------------------------------------------------------------------------------

def test_the_purge_holds_a_plan_that_still_renews_then_erases_every_plan_row(
        desk, database, billing_dsn, purge_dsn, readers, caplog):
    """The 30 days pass while the cancel is still unconfirmed (Paddle refused every try). The
    break it catches: an account erased while Paddle would still charge it, so no one could ever
    cancel it; the warning missing, or naming the reader or their subscription; once Paddle
    confirms, the plan's rows (reader_plans, paddle_events, paddle_checkouts) left behind, or
    another reader's taken; Paddle asked to delete anything."""
    client, tokens, standin, app = desk
    a, b, c = readers["a"], readers["b"], readers["c"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    standin.fail = ("refuse", 409, "subscription_locked_renewal")
    delete(client, tokens, "a")
    delete(client, tokens, "c")                       # no plan at all
    for owner in (a, c):
        due_now(database, owner)
    a_rows, b_rows = rows_of(database, a), rows_of(database, b)
    assert a_rows == {"reader_plans": 1, "paddle_events": 1, "paddle_checkouts": 1}
    caplog.set_level(logging.WARNING)

    purge = PostgresAccountPurge(purge_dsn)
    result = purge_due_accounts(purge, app.state.object_storage)
    assert (result.erased, result.unfinished, result.held) == (1, 0, 1)
    assert rows_of(database, a) == a_rows
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT status, erase_after IS NOT NULL FROM users WHERE id = %s",
                            (a,)).fetchone() == ("deleting", True)
    assert HELD in caplog.text
    for secret in (str(a), sub_id("a"), row(database, a)["paddle_customer_id"]):
        assert secret not in caplog.text, secret

    # Paddle confirms through the regular check; the next purge erases every plan row.
    standin.fail = None
    regular_check(billing_dsn, standin)
    assert not renews_at_paddle(standin, sub_id("a"))
    caplog.clear()
    result = purge_due_accounts(purge, app.state.object_storage)
    assert (result.erased, result.unfinished, result.held) == (1, 0, 0)
    assert HELD not in caplog.text
    assert rows_of(database, a) == {"reader_plans": 0, "paddle_events": 0, "paddle_checkouts": 0}
    assert rows_of(database, b) == b_rows
    assert renews_at_paddle(standin, sub_id("b"))
    assert all(method in ("GET", "POST", "PATCH") for method, *_rest in standin.requests)
    assert sub_id("a") in standin.subscriptions and MYSIGNER_SUB in standin.subscriptions
    assert MYSIGNER_CUSTOMER not in json.dumps([r[:3] for r in standin.requests])


# ---- the copy of one's data -----------------------------------------------------------------------

def test_your_plan_json_holds_the_gift_and_plan_and_nothing_secret(desk, database, readers):
    """The break it catches: the copy missing the gift, or the plan's kind, status and dates;
    holding Paddle's ids, a checkout token or its digest, an amount, or anything of another
    reader; or still naming the removed free weeks."""
    client, tokens, standin, _app = desk
    a, b = readers["a"], readers["b"]
    custom = subscribe(client, tokens, standin, "a", a, sub_id("a"), which="yearly")
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '2099-06-30'::date)", (b,))
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    delete(client, tokens, "a")

    _become(client, tokens, "a")                      # waiting to be erased, and may take a copy
    response = client.get("/api/account/export")
    assert response.status_code == 200, response.text
    files = {name: zipfile.ZipFile(io.BytesIO(response.content)).read(name).decode("utf-8")
             for name in zipfile.ZipFile(io.BytesIO(response.content)).namelist()}
    mine = json.loads(files["your plan.json"])
    assert set(mine) == {"gift", "plan", "not_to_renew_since_you_asked_to_delete_your_account"}
    assert mine["gift"] is None
    assert set(mine["plan"]) == {"kind", "status_at_paddle", "first_paid", "this_period_ends",
                                 "ends", "ended_by_a_refund", "sold_by"}
    assert mine["plan"]["ended_by_a_refund"] is None
    assert (mine["plan"]["kind"], mine["plan"]["status_at_paddle"]) == ("yearly", "active")
    assert datetime.fromisoformat(mine["plan"]["ends"]) == period_end(standin, sub_id("a"))
    assert mine["plan"]["first_paid"] is not None
    assert mine["not_to_renew_since_you_asked_to_delete_your_account"] is not None
    assert "your plan.json" in files["READ ME.txt"]
    assert "free weeks" not in files["READ ME.txt"]
    everything = "\n".join(files.values())
    for secret in ("sub_", "ctm_", "pri_", "txn_", custom["mothli_checkout"], str(a), str(b),
                   "Reader B", "token", "digest", "$", "usd"):
        assert secret not in everything, secret

    _become(client, tokens, "b")                      # B's own copy, from B's session
    response = client.get("/api/account/export")
    theirs = json.loads(zipfile.ZipFile(io.BytesIO(response.content)).read("your plan.json"))
    assert theirs["gift"] == "Mothli on the house until the end of 2099-06-30 (UTC)"
    assert theirs["plan"]["kind"] == "monthly"
    assert theirs["not_to_renew_since_you_asked_to_delete_your_account"] is None


def test_the_ending_route_is_the_readers_own_and_signed_in(desk, readers):
    """The break it catches: the waiting screen's read answering without a session, for an
    account named in the address, or with an id or an amount in it."""
    client, tokens, standin, _app = desk
    subscribe(client, tokens, standin, "a", readers["a"], sub_id("a"))
    delete(client, tokens, "a")
    client.cookies.clear()
    assert client.get("/api/billing/ending").status_code == 401
    _become(client, tokens, "b")
    assert client.get(f"/api/billing/ending?owner_id={readers['a']}").status_code == 422
    assert ending(client, tokens, "b") == {"wont_renew": False, "ends": None}
    seen = ending(client, tokens, "a")
    assert set(seen) == {"wont_renew", "ends"}
    assert "sub_" not in json.dumps(seen) and str(readers["a"]) not in json.dumps(seen)
