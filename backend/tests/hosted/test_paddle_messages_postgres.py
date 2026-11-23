"""Paddle's messages (ADR 0045 step 3), on real PostgreSQL.

Paddle calls ``POST /api/billing/paddle`` with a signed message whenever a subscription changes.
Only a message whose signature checks, signed within the time window, about one of Mothli's own
prices, can change a reader's plan; it is applied in one transaction on the billing role (row-level
security enforced) and recorded once by its id. Paddle's account is shared with another product of
the owner's (MySigner here), whose messages arrive at the same address and must change nothing and
leave nothing behind.

Every message is built and signed in the test: no Paddle account, sandbox or network is used.
Through the hosted HTTP app, with the billing role granted exactly what the deployment's role
script grants it. Stand-in models only, where a reply is asked for at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import conninfo, sql

from app.hosted.companion.spend import _DAY, _MONTH, set_spend_limits
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_free_weeks_postgres as free_weeks
from tests.hosted.test_companion_spend_postgres import spend
from tests.hosted.test_companion_stream_postgres import (
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
)
from tests.hosted.test_deploy import load_roles
from tests.hosted.test_free_weeks_postgres import refused, shelve, stamp

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
readers = read_map_gate.readers
make_app = free_weeks.make_app

PATH = "/api/billing/paddle"
# Stand-ins in Paddle's shapes; none is a real secret or id.
SECRET = "pdl_ntfset_" + "teststandin" * 3


def paddle_id(prefix: str, tag: str) -> str:
    return prefix + (tag + "0" * 26)[:26]


MONTHLY = paddle_id("pri_", "mothlimonthly")
YEARLY = paddle_id("pri_", "mothliyearly7999")
YEARLY_NEW = paddle_id("pri_", "mothliyearly8999")
MYSIGNER = paddle_id("pri_", "mysignerpro")
PADDLE = {"paddle_webhook_secret": SECRET, "paddle_price_monthly": MONTHLY,
          "paddle_price_yearly": f"{YEARLY_NEW},{YEARLY}"}


def stamp_of(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def message(sub: str, status: str = "active", *, reader=None, event_type="subscription.updated",
            prices=(MONTHLY,), at: datetime | None = None, event: str | None = None,
            scheduled: dict | None = None, customer: str = paddle_id("ctm_", "reader"),
            custom: dict | None = None) -> dict:
    """A subscription message as Paddle sends it (developer.paddle.com/webhooks/subscriptions)."""
    at = at or datetime.now(UTC)
    data = {
        "id": sub, "status": status, "customer_id": customer, "address_id": "add_x",
        "currency_code": "USD", "collection_mode": "automatic",
        "custom_data": custom if custom is not None else (
            None if reader is None else {"mothli_reader": str(reader)}),
        "items": [{"status": "active", "quantity": 1,
                   "price": {"id": price, "product_id": paddle_id("pro_", "x"),
                             "custom_data": {"plan": "monthly"}},
                   "product": {"id": paddle_id("pro_", "x"), "custom_data": {"app": "mothli"}}}
                  for price in prices],
        "current_billing_period": None if status in ("paused", "canceled") else {
            "starts_at": stamp_of(at), "ends_at": stamp_of(at + timedelta(days=30))},
        "scheduled_change": scheduled,
        "started_at": stamp_of(at), "first_billed_at": stamp_of(at),
    }
    return {"event_id": event or paddle_id("evt_", uuid.uuid4().hex),
            "event_type": event_type, "occurred_at": stamp_of(at),
            "notification_id": paddle_id("ntf_", uuid.uuid4().hex), "data": data}


def signature(body: bytes, *, ts: int | None = None, secret: str = SECRET) -> str:
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={digest}"


def body_of(payload: dict) -> bytes:
    # Paddle's own spacing is kept as sent: the signature is over these exact bytes.
    return json.dumps(payload, indent=2).encode()


def post(client, payload: dict | bytes, header: str | None = "sign"):
    body = payload if isinstance(payload, bytes) else body_of(payload)
    headers = {"Content-Type": "application/json"}
    if header == "sign":
        headers["Paddle-Signature"] = signature(body)
    elif header is not None:
        headers["Paddle-Signature"] = header
    return client.post(PATH, content=body, headers=headers)


def sub_id(tag: str) -> str:
    return paddle_id("sub_", tag)


@pytest.fixture()
def billing_dsn(database: str, admin_dsn: str):
    """A login role holding exactly what ``scripts/hosted_roles.py`` grants the billing role."""
    roles = load_roles()
    billing = next(role for role in roles.RUNTIME_ROLES if role.key == "billing")
    assert not billing.bypass_rls
    prefix = f"paddle{uuid.uuid4().hex[:12]}"
    name = sql.Identifier(roles.role_name(prefix, billing))
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOINHERIT NOBYPASSRLS").format(name))
    with psycopg.connect(database, autocommit=True) as conn:
        for statement in roles.grant_statements(prefix, billing):
            conn.execute(statement)
    try:
        yield conninfo.make_conninfo(database, user=roles.role_name(prefix, billing))
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(name))
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(name))


@pytest.fixture()
def paddle(make_app, billing_dsn):
    """The hosted app with Paddle's messages on; its client."""
    client, _tokens, _app = make_app(**PADDLE, hosted_billing_dsn=billing_dsn)
    return client


def plan_of(dsn: str, owner: uuid.UUID) -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state, why, paid_by FROM reader_plan_state(%s, 14, 2.10)",
                            (owner,)).fetchone()


def row_of(dsn: str, owner: uuid.UUID):
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT to_jsonb(plan) - 'updated_at' FROM reader_plans AS plan"
                             " WHERE owner_id=%s", (owner,)).fetchone()
    return None if found is None else found[0]


def events(dsn: str) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT event_id, event_type, owner_id, outcome FROM paddle_events"
                            " ORDER BY received_at, event_id").fetchall()


def everything(dsn: str) -> tuple:
    """Every plan row and every recorded message: what a refused message must leave as it was."""
    with psycopg.connect(dsn) as conn:
        return (conn.execute("SELECT to_jsonb(plan) FROM reader_plans AS plan"
                             " ORDER BY owner_id").fetchall(),
                conn.execute("SELECT to_jsonb(event) FROM paddle_events AS event"
                             " ORDER BY event_id").fetchall())


# ---- the signature ----------------------------------------------------------------------------

def test_a_message_without_a_good_signature_is_refused_and_writes_nothing(paddle, database,
                                                                          readers):
    """The break it catches: a forged message making someone paying: no signature, a malformed
    one, one made with another secret, a valid signature over a different body, or none of
    several ``h1`` values matching. A signature that matches one ``h1`` of several (a secret
    being rotated) is accepted."""
    a = readers["a"]
    payload = message(sub_id("a"), reader=a)
    body = body_of(payload)
    other = body_of(message(sub_id("a"), "past_due", reader=a))
    good = signature(body)
    ts = good.split(";")[0]
    before = everything(database)
    for header in (None, "", "ts=;h1=", "h1=" + good.split("h1=")[1], ts,
                   signature(body, secret="pdl_ntfset_" + "another" * 4),
                   signature(other), f"{ts};h1={'0' * 64};h1={'f' * 64}",
                   good.replace(ts, f"ts={int(ts[3:]) + 1}"), good.upper()):
        response = post(paddle, body, header)
        assert response.status_code == 400, header
        assert response.content == b""
    # The same body re-serialized (different spacing) no longer matches the signature.
    response = paddle.post(PATH, content=json.dumps(payload).encode(),
                           headers={"Paddle-Signature": good})
    assert response.status_code == 400
    assert everything(database) == before and plan_of(database, a)[0] == "new"

    rotated = f"{ts};h1={'0' * 64};h1={good.split('h1=')[1]}"
    assert post(paddle, body, rotated).status_code == 200
    assert plan_of(database, a) == ("paying", None, "paddle")


def test_an_old_or_future_timestamp_is_refused_even_with_a_valid_signature(paddle, database,
                                                                            readers):
    """The break it catches: a valid message captured once and replayed later (signed with its
    old time), or one signed far in the future, accepted. Inside the window it is accepted."""
    a = readers["a"]
    body = body_of(message(sub_id("a"), reader=a))
    now = int(time.time())
    before = everything(database)
    for ts in (now - 301, now - 3600, now - 86400, now + 301):
        assert post(paddle, body, signature(body, ts=ts)).status_code == 400, ts
    assert everything(database) == before
    assert post(paddle, body, signature(body, ts=now - 240)).status_code == 200
    assert plan_of(database, a) == ("paying", None, "paddle")


def test_with_no_secret_the_route_does_not_exist_and_nothing_is_written(make_app, database,
                                                                        readers, billing_dsn):
    """The break it catches: a server without PADDLE_WEBHOOK_SECRET that still takes Paddle's
    messages (with an empty key anyone could sign) and makes someone paying."""
    client, _tokens, _app = make_app(paddle_price_monthly=MONTHLY,
                                     hosted_billing_dsn=billing_dsn)
    body = body_of(message(sub_id("a"), reader=readers["a"]))
    before = everything(database)
    for header in (signature(body, secret=""), signature(body), None):
        response = post(client, body, header)
        assert response.status_code in (404, 405), response.status_code
    # An empty line in the settings is no secret either.
    client, _tokens, _app = make_app(paddle_webhook_secret="", paddle_price_monthly=MONTHLY,
                                     hosted_billing_dsn=billing_dsn)
    assert post(client, body, signature(body, secret="")).status_code in (404, 405)
    assert everything(database) == before


# ---- duplicates and order ---------------------------------------------------------------------

def test_a_repeated_message_is_answered_and_changes_nothing(paddle, database, readers):
    """The break it catches: Paddle delivering one message twice (which it says it may) applied
    twice or recorded twice; or a repeat carrying the same id over a different body (a later
    state) applied."""
    a = readers["a"]
    first = message(sub_id("a"), reader=a)
    assert post(paddle, first).status_code == 200
    applied = (row_of(database, a), events(database))
    assert post(paddle, first).status_code == 200
    again = message(sub_id("a"), "canceled", reader=a, event=first["event_id"],
                    at=datetime.now(UTC) + timedelta(minutes=5))
    assert post(paddle, again).status_code == 200
    assert (row_of(database, a), events(database)) == applied
    assert [event[3] for event in events(database)] == ["applied"]
    assert plan_of(database, a) == ("paying", None, "paddle")


def test_an_older_message_after_a_newer_one_changes_nothing(paddle, database, readers):
    """The break it catches: Paddle's messages arriving out of order (it does not keep the
    order) and an old state written over a newer one: a cancelled reader made paying again by
    the late ``subscription.created``."""
    a = readers["a"]
    now = datetime.now(UTC)
    assert post(paddle, message(sub_id("a"), "canceled", reader=a, at=now)).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    created = message(sub_id("a"), "active", reader=a, event_type="subscription.created",
                      at=now - timedelta(minutes=2))
    assert post(paddle, created).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    assert [event[3] for event in events(database)] == ["applied", "older"]
    # The same time is not newer either.
    assert post(paddle, message(sub_id("a"), "active", reader=a, at=now)).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)


# ---- the states of section 1 ------------------------------------------------------------------

def test_each_status_gives_the_state_of_section_1(paddle, database, readers):
    """The break it catches: a status read the wrong way. Active and trialing are paying;
    past_due (Paddle retrying a payment) is paying; a plan cancelled to the end of what was paid
    (active with a scheduled cancel) is paying until that date, ended with why "plan" after it;
    paused and canceled are ended with why "plan"; a pause scheduled ahead is paying until it."""
    a = readers["a"]
    now = datetime.now(UTC)
    later = stamp_of(now + timedelta(days=12))
    steps = [
        (message(sub_id("a"), "active", reader=a), ("paying", None, "paddle")),
        (message(sub_id("a"), "past_due", reader=a), ("paying", None, "paddle")),
        (message(sub_id("a"), "trialing", reader=a), ("paying", None, "paddle")),
        (message(sub_id("a"), "active", reader=a,
                 scheduled={"action": "cancel", "effective_at": later, "resume_at": None}),
         ("paying", None, "paddle")),
        (message(sub_id("a"), "active", reader=a,
                 scheduled={"action": "cancel", "effective_at": stamp_of(now - timedelta(
                     hours=1)), "resume_at": None}), ("ended", "plan", None)),
        (message(sub_id("a"), "active", reader=a), ("paying", None, "paddle")),   # kept the plan
        (message(sub_id("a"), "active", reader=a,
                 scheduled={"action": "pause", "effective_at": later, "resume_at": None}),
         ("paying", None, "paddle")),
        (message(sub_id("a"), "paused", reader=a), ("ended", "plan", None)),
        (message(sub_id("a"), "active", reader=a,
                 scheduled={"action": "resume", "effective_at": later, "resume_at": None}),
         ("paying", None, "paddle")),
        (message(sub_id("a"), "canceled", reader=a), ("ended", "plan", None)),
    ]
    for step, (payload, expected) in enumerate(steps):
        payload["occurred_at"] = stamp_of(now + timedelta(seconds=step))
        assert post(paddle, payload).status_code == 200
        assert plan_of(database, a) == expected, (step, payload["data"]["status"])
    row = row_of(database, a)
    assert (row["paddle_subscription_id"], row["paddle_status"], row["ends_at"],
            row["period_ends_at"]) == (sub_id("a"), "canceled", None, None)
    with psycopg.connect(database) as conn:
        dates = conn.execute("SELECT paddle_status, plan_ends_at FROM reader_plan_state"
                             "(%s, 14, 2.10)", (a,)).fetchone()
    assert dates == ("canceled", None)


def test_a_reader_who_paid_and_stopped_is_never_back_in_the_free_weeks(paddle, tenant_dsn,
                                                                       database, readers):
    """The break it catches: a reader who subscribed during their free weeks and cancelled
    given the rest of the free weeks (and the free ceiling) back; or a gift not winning over a
    cancelled plan while it lasts."""
    a = readers["a"]
    book = shelve(database, a)
    stamp(tenant_dsn, a)
    assert plan_of(database, a) == ("free", None, None)
    now = datetime.now(UTC)
    post(paddle, message(sub_id("a"), reader=a, at=now))
    assert plan_of(database, a) == ("paying", None, "paddle")
    post(paddle, message(sub_id("a"), "canceled", reader=a, at=now + timedelta(seconds=1)))
    assert plan_of(database, a) == ("ended", "plan", None)
    # Not held by the free ceiling: the free weeks are not theirs any more.
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=100, free_usd=Decimal("0.05"))
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.06)
    spend(chat, book, 0.04)
    refused(chat, book, "day", _DAY)
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (a,))
    assert plan_of(database, a) == ("paying", None, "gift")


def test_a_cancelled_subscription_then_a_new_one_for_the_same_reader(paddle, database, readers):
    """The break it catches: a reader who cancelled and subscribed again kept ended because the
    old subscription is stored; or a late message about the old subscription (cancelled, or a
    stale active one) taking the place of the new live one."""
    a = readers["a"]
    now = datetime.now(UTC)
    post(paddle, message(sub_id("old"), reader=a, at=now))
    post(paddle, message(sub_id("old"), "canceled", reader=a, at=now + timedelta(seconds=1)))
    assert plan_of(database, a) == ("ended", "plan", None)
    post(paddle, message(sub_id("new"), reader=a, event_type="subscription.created",
                         at=now + timedelta(seconds=2)))
    assert plan_of(database, a) == ("paying", None, "paddle")
    assert row_of(database, a)["paddle_subscription_id"] == sub_id("new")
    # The old one's messages, late or repeated in another form, change nothing.
    post(paddle, message(sub_id("old"), "canceled", reader=a, at=now + timedelta(seconds=3)))
    post(paddle, message(sub_id("old"), "active", reader=a, at=now + timedelta(seconds=1, )))
    assert plan_of(database, a) == ("paying", None, "paddle")
    assert row_of(database, a)["paddle_subscription_id"] == sub_id("new")
    assert [event[3] for event in events(database)] == [
        "applied", "applied", "applied", "other_subscription", "older"]
    # A new subscription made before the old one's cancel arrived: the live one stays.
    b = readers["b"]
    post(paddle, message(sub_id("b1"), reader=b, at=now))
    post(paddle, message(sub_id("b2"), reader=b, at=now + timedelta(seconds=2)))
    post(paddle, message(sub_id("b1"), "canceled", reader=b, at=now + timedelta(seconds=1)))
    post(paddle, message(sub_id("b1"), "canceled", reader=b, at=now + timedelta(seconds=3)))
    assert row_of(database, b)["paddle_subscription_id"] == sub_id("b2")
    assert plan_of(database, b) == ("paying", None, "paddle")


# ---- which account --------------------------------------------------------------------------

def test_an_unknown_or_erased_account_is_recorded_and_ignored(paddle, database, readers):
    """The break it catches: a message naming an account that does not exist, one being
    erased, or none, making a row (or failing, so Paddle retries for two days); or the message
    recorded with that account's id. It is recorded without an account, and answered."""
    stranger = uuid.uuid4()
    erased = readers["b"]
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE users SET status = 'deleted', deleted_at = now() WHERE id = %s",
                     (erased,))
    for named in ({"mothli_reader": str(stranger)}, {"mothli_reader": str(erased)},
                  {"mothli_reader": "not-an-id"}, {}, {"mothli_reader": None}):
        assert post(paddle, message(sub_id("x"), custom=named)).status_code == 200
    payload = message(sub_id("y"))
    payload["data"]["custom_data"] = None
    assert post(paddle, payload).status_code == 200
    with psycopg.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM reader_plans").fetchone()[0] == 0
    recorded = events(database)
    assert len(recorded) == 6
    assert {(event[2], event[3]) for event in recorded} == {(None, "no_account")}


def test_a_message_for_reader_a_never_changes_reader_b(paddle, billing_dsn, database, readers):
    """The break it catches: a message reaching another reader's plan: custom data naming B on
    A's subscription, a new subscription naming A changing B, or the billing role, set to one
    reader, reading or writing another's row or recorded messages."""
    a, b = readers["a"], readers["b"]
    now = datetime.now(UTC)
    post(paddle, message(sub_id("b"), reader=b, at=now))
    b_row = row_of(database, b)
    post(paddle, message(sub_id("a"), reader=a, at=now))
    assert row_of(database, b) == b_row
    # A's subscription, later claiming B in its custom data: still A's.
    post(paddle, message(sub_id("a"), "canceled", reader=b, at=now + timedelta(seconds=1)))
    assert plan_of(database, a) == ("ended", "plan", None)
    assert row_of(database, b) == b_row and plan_of(database, b) == ("paying", None, "paddle")
    # B's subscription named on a message for A: B's plan, B's row.
    post(paddle, message(sub_id("b"), "past_due", reader=a, at=now + timedelta(seconds=2)))
    assert row_of(database, b)["paddle_status"] == "past_due"
    assert row_of(database, a)["paddle_status"] == "canceled"
    owners = {event[2] for event in events(database)}
    assert owners == {a, b}

    with psycopg.connect(billing_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        assert conn.execute("SELECT owner_id FROM reader_plans").fetchall() == [(a,)]
        assert {row[0] for row in conn.execute("SELECT owner_id FROM paddle_events")} == {a}
        assert conn.execute("UPDATE reader_plans SET paddle_status = 'active'"
                            " WHERE owner_id = %s", (b,)).rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO paddle_events (event_id, event_type, occurred_at,"
                         " owner_id, outcome) VALUES (%s, 'subscription.updated', now(), %s,"
                         " 'applied')", (paddle_id("evt_", "forged"), b))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO reader_plans (owner_id, paddle_subscription_id,"
                         " paddle_status, paddle_occurred_at) VALUES (%s, %s, 'active', now())",
                         (b, sub_id("forged")))
    assert row_of(database, b)["paddle_status"] == "past_due"


# ---- the shared Paddle account ------------------------------------------------------------------

def test_messages_about_another_products_prices_change_nothing_and_leave_nothing(
        paddle, database, readers, caplog):
    """The break it catches: the owner's other product (MySigner) sharing the Paddle account,
    and its customer's payment making a Mothli reader paying, or anything about it kept. Its
    messages are answered 200 and leave no row, no recorded id and no log of an id: a MySigner
    subscription naming a Mothli reader in its custom data; one carrying Mothli's own product
    and plan custom data; one with a Mothli price beside a MySigner price; a MySigner
    subscription whose id later appears; and a Mothli price on a message that is not about a
    subscription (a transaction, an adjustment)."""
    a, b = readers["a"], readers["b"]
    before = everything(database)
    caplog.set_level(logging.DEBUG)
    shared = [
        message(sub_id("mysigner"), reader=a, prices=(MYSIGNER,)),
        message(sub_id("mysigner"), reader=a, prices=(MYSIGNER,),
                custom={"mothli_reader": str(a), "app": "mothli", "plan": "yearly"}),
        message(sub_id("mysigner"), reader=a, prices=(MONTHLY, MYSIGNER)),
        message(sub_id("mysigner"), reader=a, prices=(MYSIGNER, YEARLY)),
        message(sub_id("mysigner"), reader=a, prices=()),
        message(sub_id("mysigner"), "canceled", reader=b, prices=(MYSIGNER,),
                event_type="subscription.canceled"),
    ]
    for event_type in ("transaction.completed", "transaction.payment_failed",
                       "adjustment.created", "customer.created"):
        shared.append(message(sub_id("mothli"), reader=a, event_type=event_type))
    for payload in shared:
        response = post(paddle, payload)
        assert (response.status_code, response.content) == (200, b"")
    assert everything(database) == before
    assert plan_of(database, a)[0] == "new" and plan_of(database, b)[0] == "new"
    for payload in shared:
        assert payload["event_id"] not in caplog.text
    assert sub_id("mysigner") not in caplog.text and str(a) not in caplog.text

    # A Mothli reader who is also a MySigner customer: MySigner's messages never touch their
    # Mothli plan, before or after it exists.
    now = datetime.now(UTC)
    post(paddle, message(sub_id("mothli"), reader=a, prices=(YEARLY,), at=now))
    mothli = row_of(database, a)
    recorded = events(database)
    post(paddle, message(sub_id("mysigner"), "canceled", reader=a, prices=(MYSIGNER,),
                         at=now + timedelta(seconds=5)))
    post(paddle, message(sub_id("mothli"), "canceled", reader=a, prices=(MYSIGNER,),
                         at=now + timedelta(seconds=5)))
    assert row_of(database, a) == mothli and events(database) == recorded
    assert plan_of(database, a) == ("paying", None, "paddle")


def test_every_one_of_mothlis_prices_is_accepted_old_and_new(paddle, database, readers):
    """The break it catches: readers on the launch yearly price stopped once a new yearly price
    is added (the setting holds both), or the monthly price not recognised."""
    now = datetime.now(UTC)
    for owner, price in ((readers["a"], YEARLY), (readers["b"], YEARLY_NEW)):
        post(paddle, message(sub_id(str(owner)[:8]), reader=owner, prices=(price,), at=now))
        assert plan_of(database, owner) == ("paying", None, "paddle"), price
    post(paddle, message(sub_id(str(readers["a"])[:8]), reader=readers["a"],
                         prices=(MONTHLY,), at=now + timedelta(seconds=1)))
    assert plan_of(database, readers["a"]) == ("paying", None, "paddle")


def test_a_mothli_message_that_cannot_be_read_is_refused_and_writes_nothing(paddle, database,
                                                                             readers):
    """The break it catches: a correctly signed Mothli message with a status, id or time Mothli
    does not know written anyway (or crashing the route)."""
    a = readers["a"]
    before = everything(database)
    broken = []
    for field, value in (("status", "frozen"), ("id", "sub_123"), ("customer_id", None)):
        payload = message(sub_id("a"), reader=a)
        payload["data"][field] = value
        broken.append(payload)
    for field, value in (("event_id", "evt_1"), ("occurred_at", "yesterday"),
                         ("occurred_at", "2026-11-23T10:00:00")):
        payload = message(sub_id("a"), reader=a)
        payload[field] = value
        broken.append(payload)
    for payload in broken:
        assert post(paddle, payload).status_code == 400
    assert post(paddle, b"{not json").status_code == 400
    assert post(paddle, b"x" * (300 * 1024)).status_code == 413
    assert everything(database) == before


def test_the_logs_never_hold_the_secret_the_signature_an_id_or_the_payload(paddle, readers,
                                                                          caplog):
    caplog.set_level(logging.DEBUG)
    a = readers["a"]
    payload = message(sub_id("a"), reader=a, customer=paddle_id("ctm_", "secretcustomer"))
    body = body_of(payload)
    header = signature(body)
    paddle.post(PATH, content=body, headers={"Paddle-Signature": header})
    paddle.post(PATH, content=body, headers={"Paddle-Signature": header.replace("ts=", "ts=1")})
    text = caplog.text
    for secret in (SECRET, header.split("h1=")[1], payload["event_id"], sub_id("a"), str(a),
                   paddle_id("ctm_", "secretcustomer"), "mothli_reader"):
        assert secret not in text, secret


# ---- the billing role -------------------------------------------------------------------------

def test_the_billing_role_reads_and_writes_only_its_two_tables(billing_dsn, database, readers):
    """The break it catches: the role used by an unauthenticated route able to read any reader's
    books, conversations, spend or account, write anything but a plan and a recorded message,
    delete or rewrite a recorded message, give a gift, or bypass row-level security."""
    a = readers["a"]
    with psycopg.connect(database) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")]
        first_column = dict(conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND ordinal_position = 1").fetchall())
        bypass = conn.execute("SELECT rolbypassrls OR rolsuper FROM pg_roles"
                              " WHERE rolname = %s", (conninfo.conninfo_to_dict(
                                  billing_dsn)["user"],)).fetchone()[0]
    assert not bypass
    assert {"reader_plans", "paddle_events", "users", "books", "cost_ledger"} <= set(tables)
    with psycopg.connect(billing_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        for table in tables:
            name = sql.Identifier("public", table)
            if table not in ("reader_plans", "paddle_events"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(name))
            statements = [sql.SQL("DELETE FROM {}").format(name),
                          sql.SQL("TRUNCATE {}").format(name)]
            if table != "reader_plans":
                column = sql.Identifier(first_column[table])
                statements.append(sql.SQL("UPDATE {} SET {} = {}").format(name, column, column))
            if table not in ("reader_plans", "paddle_events"):
                statements.append(sql.SQL("INSERT INTO {} DEFAULT VALUES").format(name))
            for statement in statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        for call, params in (("SELECT set_gift(%s, '9999-12-31'::date)", (a,)),
                             ("SELECT start_free_weeks()", ()),
                             ("SELECT * FROM reader_plan_state(%s, 14, 2.10)", (a,))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(call, params)
    with psycopg.connect(database) as conn:
        assert not conn.execute("SELECT has_function_privilege('public',"
                                " 'public.paddle_reader(text,uuid)', 'EXECUTE')").fetchone()[0]


def test_the_tenant_role_cannot_write_a_paddle_plan_or_find_one(tenant_dsn, database, readers):
    """The break it catches: a reader's session able to make itself paying by writing the
    Paddle columns, recording a message, or looking up which account holds a subscription."""
    a = readers["a"]
    with psycopg.connect(tenant_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        conn.execute("SELECT start_free_weeks()")
        for statement, params in (
                ("UPDATE reader_plans SET paddle_subscription_id = %s, paddle_status = 'active',"
                 " paddle_occurred_at = now()", (sub_id("a"),)),
                ("SELECT * FROM paddle_events", ()),
                ("INSERT INTO paddle_events (event_id, event_type, occurred_at, owner_id, outcome)"
                 " VALUES (%s, 'subscription.created', now(), %s, 'applied')",
                 (paddle_id("evt_", "t"), a)),
                ("SELECT paddle_reader(%s, %s)", (sub_id("a"), a))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
    assert plan_of(database, a) == ("free", None, None)


def test_a_paying_readers_month_line_is_todays(paddle, tenant_dsn, database, readers):
    """The break it catches: a reader paying through Paddle held by the free ceiling, or by
    nothing; they are held by the day and month guards with today's lines."""
    a = readers["a"]
    book = shelve(database, a)
    post(paddle, message(sub_id("a"), reader=a))
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=Decimal("0.20"),
                     platform_usd_per_day=100, free_usd=Decimal("0.01"))
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.09)                                 # past the free ceiling: not applied
    spend(chat, book, 0.01)
    refused(chat, book, "day", _DAY)
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=Decimal("0.10"),
                     platform_usd_per_day=100, free_usd=Decimal("0.01"))
    refused(chat, book, "month", _MONTH)
