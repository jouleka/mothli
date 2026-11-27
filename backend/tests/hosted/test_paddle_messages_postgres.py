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

import base64
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

from app.hosted.companion.spend import (
    _DAY,
    _DAY_PAST_MONTH,
    FIRST_PAID_MONTH,
    back_on,
    first_month_line,
    set_spend_limits,
)
from app.hosted.companion.store import HostedCompanionCatalog
from app.hosted.limits import update_limits
from app.hosted.tenant.models import OwnerId
from tests.hosted import test_companion_read_map_postgres as read_map_gate
from tests.hosted import test_mothli_gate_postgres as mothli_gate
from tests.hosted.test_companion_spend_postgres import spend
from tests.hosted.test_companion_stream_postgres import (
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
)
from tests.hosted.test_deploy import load_roles
from tests.hosted.test_mothli_gate_postgres import gated, month_wall, refused, shelve

pytestmark = pytest.mark.postgres

admin_dsn = read_map_gate.admin_dsn
database = read_map_gate.database
tenant_dsn = read_map_gate.tenant_dsn
# New readers: Paddle's messages are what make them paying here.
readers = read_map_gate.new_readers
make_app = mothli_gate.make_app

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


def checkout_token(reader, sub: str) -> str:
    """The checkout token a message names ``reader`` with, in the shape the server makes (43
    URL-safe characters): one per reader and subscription, so a second subscription for the same
    reader comes from a checkout of its own (ADR 0045 step 4)."""
    digest = hashlib.sha256(f"{reader}:{sub}".encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def message(sub: str, status: str = "active", *, reader=None, event_type="subscription.updated",
            prices=(MONTHLY,), at: datetime | None = None, event: str | None = None,
            scheduled: dict | None = None, customer: str = paddle_id("ctm_", "reader"),
            custom: dict | None = None) -> dict:
    """A subscription message as Paddle sends it (developer.paddle.com/webhooks/subscriptions).
    A ``reader`` is named with the checkout token of their own checkout for ``sub``, which
    ``post`` makes as the subscribe route would (unless told not to)."""
    at = at or datetime.now(UTC)
    data = {
        "id": sub, "status": status, "customer_id": customer, "address_id": "add_x",
        "currency_code": "USD", "collection_mode": "automatic",
        "custom_data": custom if custom is not None else (
            None if reader is None else {"mothli_reader": str(reader),
                                         "mothli_checkout": checkout_token(reader, sub)}),
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


def seed_checkout(dsn: str, owner, token: str, *, used: bool = False,
                  expired: bool = False) -> None:
    """A checkout the subscribe route would have made for ``owner`` (written past row-level
    security); kept once, so a token used by a first subscription stays used."""
    created = "now() - interval '8 days'" if expired else "now()"
    expires = "now() - interval '1 day'" if expired else "now() + interval '7 days'"
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO paddle_checkouts (owner_id, token_digest, created_at, expires_at,"
            f" used_at) VALUES (%s, %s, {created}, {expires}, CASE WHEN %s THEN now() END)"
            " ON CONFLICT DO NOTHING",
            (owner, hashlib.sha256(token.encode("ascii")).digest(), used))


def _seed_from(dsn: str, body: bytes) -> None:
    try:
        custom = json.loads(body)["data"]["custom_data"]
        owner = uuid.UUID(custom["mothli_reader"])
        token = custom["mothli_checkout"]
    except (ValueError, KeyError, TypeError):
        return
    if isinstance(token, str) and len(token) == 43:
        seed_checkout(dsn, owner, token)


def post(client, payload: dict | bytes, header: str | None = "sign", *, seed: bool = True):
    body = payload if isinstance(payload, bytes) else body_of(payload)
    # The checkout behind a message naming a reader, as the subscribe route makes it.
    if seed and getattr(client, "checkouts_dsn", None):
        _seed_from(client.checkouts_dsn, body)
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
def paddle(make_app, billing_dsn, database):
    """The hosted app with Paddle's messages on; its client. A message ``post`` sends naming a
    reader comes with that reader's checkout, unless ``seed=False``."""
    client, _tokens, _app = make_app(**PADDLE, hosted_billing_dsn=billing_dsn)
    client.checkouts_dsn = database
    return client


def plan_of(dsn: str, owner: uuid.UUID) -> tuple:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT state, why, paid_by FROM reader_plan_state(%s)",
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
                                                                            readers, monkeypatch):
    """The break it catches: a valid message captured once and replayed later (signed with its
    old time), or one signed in the future, accepted; or one just inside the window refused.
    The route's clock is held still, so each edge of the 300 seconds is exact on any runner."""
    from types import SimpleNamespace

    from app.hosted import billing

    now = int(time.time())
    monkeypatch.setattr(billing, "time", SimpleNamespace(time=lambda: float(now)))
    a = readers["a"]
    body = body_of(message(sub_id("a"), reader=a))
    before = everything(database)
    for ts in (now - 301, now - 3600, now - 86400, now + 301, now + 3600):
        assert post(paddle, body, signature(body, ts=ts)).status_code == 400, ts - now
    assert everything(database) == before
    assert post(paddle, body, signature(body, ts=now - 300)).status_code == 200
    assert plan_of(database, a) == ("paying", None, "paddle")
    # The other edge: the same message again (a repeat, answered and ignored), signed at the
    # window's future end, still passes the time check.
    assert post(paddle, body, signature(body, ts=now + 300)).status_code == 200


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
        dates = conn.execute("SELECT paddle_status, plan_ends_at FROM reader_plan_state(%s)",
                             (a,)).fetchone()
    assert dates == ("canceled", None)


def test_a_reader_who_paid_and_stopped_is_ended_and_meets_the_gate(paddle, tenant_dsn,
                                                                   database, readers):
    """The break it catches: a reader who subscribed and cancelled answered at all, or taken for
    new: ended with why "plan", they meet the gate (ADR 0045 step 5, ADR 0046), not the paying
    readers' day; or a gift not winning over a cancelled plan while it lasts."""
    a = readers["a"]
    book = shelve(database, a)
    assert plan_of(database, a) == ("new", None, None)
    now = datetime.now(UTC)
    post(paddle, message(sub_id("a"), reader=a, at=now))
    assert plan_of(database, a) == ("paying", None, "paddle")
    post(paddle, message(sub_id("a"), "canceled", reader=a, at=now + timedelta(seconds=1)))
    assert plan_of(database, a) == ("ended", "plan", None)
    # The gate, with nothing spent.
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=2,
                     platform_usd_per_day=100)
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    gated(chat, book, "plan")
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
    # The old one's messages, late or repeated in another form, change nothing: it is no longer
    # stored, and the checkout it came from was used (ADR 0045 step 4), so it names no account.
    post(paddle, message(sub_id("old"), "canceled", reader=a, at=now + timedelta(seconds=3)))
    post(paddle, message(sub_id("old"), "active", reader=a, at=now + timedelta(seconds=1, )))
    assert plan_of(database, a) == ("paying", None, "paddle")
    assert row_of(database, a)["paddle_subscription_id"] == sub_id("new")
    assert [event[3] for event in events(database)] == [
        "applied", "applied", "applied", "no_account", "no_account"]
    # A plan cancelled to the end of what was paid, past that end, with Paddle's cancel message
    # not yet arrived: a new subscription takes its place at once.
    b = readers["b"]
    post(paddle, message(sub_id("b1"), reader=b, at=now, scheduled={
        "action": "cancel", "effective_at": stamp_of(now - timedelta(hours=1)),
        "resume_at": None}))
    assert plan_of(database, b) == ("ended", "plan", None)
    post(paddle, message(sub_id("b2"), reader=b, at=now + timedelta(seconds=2)))
    post(paddle, message(sub_id("b1"), "canceled", reader=b, at=now + timedelta(seconds=3)))
    assert row_of(database, b)["paddle_subscription_id"] == sub_id("b2")
    assert plan_of(database, b) == ("paying", None, "paddle")


def test_another_subscription_naming_a_paying_reader_never_displaces_theirs(paddle, database,
                                                                           readers):
    """The break it catches: someone who knows a reader's account id subscribing in their name
    (Paddle lets a checkout carry any custom data) and then cancelling, so that the reader's own
    live plan is replaced and they show as ended until their own next renewal. While a reader's
    own plan is live, any other subscription naming them, live or cancelled, is recorded as
    ``other_subscription`` and changes nothing."""
    a = readers["a"]
    now = datetime.now(UTC)
    post(paddle, message(sub_id("own"), reader=a, at=now))
    own = row_of(database, a)
    for step, status in enumerate(("active", "past_due", "canceled", "paused"), start=1):
        post(paddle, message(sub_id("stranger"), status, reader=a,
                             customer=paddle_id("ctm_", "stranger"),
                             at=now + timedelta(seconds=step)))
        assert row_of(database, a) == own, status
        assert plan_of(database, a) == ("paying", None, "paddle")
    assert [event[3] for event in events(database)] == ["applied"] + ["other_subscription"] * 4
    # Their own plan's messages still apply.
    post(paddle, message(sub_id("own"), "past_due", reader=a, at=now + timedelta(seconds=9)))
    assert row_of(database, a)["paddle_status"] == "past_due"


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


def checkout_row(dsn: str, owner, token: str):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT used_at FROM paddle_checkouts WHERE owner_id=%s"
                            " AND token_digest=%s",
                            (owner, hashlib.sha256(token.encode()).digest())).fetchone()


def test_a_new_subscription_is_a_readers_only_through_their_own_open_checkout(paddle, database,
                                                                              readers):
    """ADR 0045 step 4. The break it catches: anyone with Paddle's public client token opening a
    checkout whose custom data names a reader, and the payment binding to that reader, so they
    can make a reader paying, or later end a plan the reader made, without the server having
    made that checkout. A subscription Mothli has not stored is the named reader's only with a
    checkout token kept for that same reader and unused, however old; each case below is
    recorded with no account and changes nothing: no token, a token never made, another reader's
    token (old or new), a used token. The reader's own unused checkout makes them paying even past
    its ``expires_at`` (a payment from Paddle's late reminder email must never be lost) and is
    used up: the same token on another subscription later changes nothing."""
    a, b = readers["a"], readers["b"]
    now = datetime.now(UTC)
    before = everything(database)[0]
    b_token, b_old, used, never = (checkout_token(b, "b"), checkout_token(b, "old"),
                                   checkout_token(a, "used"), checkout_token(a, "never"))
    seed_checkout(database, b, b_token)
    seed_checkout(database, b, b_old, expired=True)
    seed_checkout(database, a, used, used=True)
    for step, custom in enumerate(({"mothli_reader": str(a)},
                                   {"mothli_reader": str(a), "mothli_checkout": never},
                                   {"mothli_reader": str(a), "mothli_checkout": b_token},
                                   {"mothli_reader": str(a), "mothli_checkout": b_old},
                                   {"mothli_reader": str(a), "mothli_checkout": used},
                                   {"mothli_reader": str(a), "mothli_checkout": "short"})):
        payload = message(sub_id("forged"), custom=custom, event_type="subscription.created",
                          at=now + timedelta(seconds=step))
        assert post(paddle, payload, seed=False).status_code == 200
    assert everything(database)[0] == before
    assert plan_of(database, a)[0] == "new" and plan_of(database, b)[0] == "new"
    assert {(event[2], event[3]) for event in events(database)} == {(None, "no_account")}
    assert checkout_row(database, b, b_token) == (None,)          # B's token is still B's

    # The reader's own checkout, made eight days ago and past its expires_at: still theirs.
    own = checkout_token(a, "own")
    seed_checkout(database, a, own, expired=True)
    custom = {"mothli_reader": str(a), "mothli_checkout": own}
    post(paddle, message(sub_id("own"), custom=custom, event_type="subscription.created",
                         at=now + timedelta(seconds=10)), seed=False)
    assert plan_of(database, a) == ("paying", None, "paddle")
    assert checkout_row(database, a, own)[0] is not None
    # Its own later messages are found by the stored subscription, the token aside.
    post(paddle, message(sub_id("own"), "past_due", custom={},
                         at=now + timedelta(seconds=11)), seed=False)
    assert row_of(database, a)["paddle_status"] == "past_due"
    # Cancelled, then the used token on another subscription: nothing.
    post(paddle, message(sub_id("own"), "canceled", custom={},
                         at=now + timedelta(seconds=12)), seed=False)
    post(paddle, message(sub_id("again"), custom=custom, event_type="subscription.created",
                         at=now + timedelta(seconds=13)), seed=False)
    assert row_of(database, a)["paddle_subscription_id"] == sub_id("own")
    assert plan_of(database, a) == ("ended", "plan", None)
    assert [event[3] for event in events(database)][-4:] == [
        "applied", "applied", "applied", "no_account"]


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
                   paddle_id("ctm_", "secretcustomer"), "mothli_reader", "mothli_checkout",
                   checkout_token(a, sub_id("a"))):
        assert secret not in text, secret


# ---- the billing role -------------------------------------------------------------------------

def test_the_billing_role_reads_and_writes_only_its_three_tables(billing_dsn, database, readers):
    """The break it catches: the role used by an unauthenticated route able to read any reader's
    books, conversations, spend or account, write anything but a plan, a recorded message and a
    checkout (step 4), delete or rewrite a recorded message, delete a checkout, give a gift, or
    bypass row-level security."""
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
    assert {"reader_plans", "paddle_events", "paddle_checkouts", "users", "books",
            "cost_ledger"} <= set(tables)
    own = ("reader_plans", "paddle_events", "paddle_checkouts")
    with psycopg.connect(billing_dsn, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.owner_id', %s, false)", (str(a),))
        for table in tables:
            name = sql.Identifier("public", table)
            if table not in own:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(name))
            statements = [sql.SQL("DELETE FROM {}").format(name),
                          sql.SQL("TRUNCATE {}").format(name)]
            if table not in ("reader_plans", "paddle_checkouts"):
                column = sql.Identifier(first_column[table])
                statements.append(sql.SQL("UPDATE {} SET {} = {}").format(name, column, column))
            if table not in own:
                statements.append(sql.SQL("INSERT INTO {} DEFAULT VALUES").format(name))
            for statement in statements:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        for call, params in (("SELECT set_gift(%s, '9999-12-31'::date)", (a,)),
                             ("SELECT * FROM reader_plan_state(%s)", (a,))):
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
        for statement, params in (
                ("UPDATE reader_plans SET paddle_subscription_id = %s, paddle_status = 'active',"
                 " paddle_occurred_at = now()", (sub_id("a"),)),
                ("SELECT * FROM paddle_events", ()),
                ("SELECT * FROM paddle_checkouts", ()),
                ("INSERT INTO paddle_checkouts (owner_id, token_digest, expires_at)"
                 " VALUES (%s, %s, now() + interval '1 day')", (a, b"x" * 32)),
                ("SELECT * FROM paddle_plans_to_check(10)", ()),
                ("INSERT INTO paddle_events (event_id, event_type, occurred_at, owner_id, outcome)"
                 " VALUES (%s, 'subscription.created', now(), %s, 'applied')",
                 (paddle_id("evt_", "t"), a)),
                ("SELECT paddle_reader(%s, %s)", (sub_id("a"), a))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, params)
    assert plan_of(database, a) == ("new", None, None)


def test_a_paying_readers_month_line_is_todays(paddle, tenant_dsn, database, readers):
    """The break it catches: a reader paying through Paddle held by nothing; past their first
    paid month they are held by the day and month guards with today's lines."""
    a = readers["a"]
    book = shelve(database, a)
    post(paddle, message(sub_id("a"), reader=a))
    move_first_paid(database, a, "now() - interval '31 days'")
    set_spend_limits(reader_usd_per_day=Decimal("0.10"), reader_usd_per_month=Decimal("0.20"),
                     platform_usd_per_day=100, reader_usd_month_ceiling=Decimal("0.20"))
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.09)
    spend(chat, book, 0.01)
    refused(chat, book, "day", _DAY)
    set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=Decimal("0.10"),
                     platform_usd_per_day=100, reader_usd_month_ceiling=Decimal("0.10"))
    refused(chat, book, "month", month_wall(database))


# ---- the first paid month (ADR 0045 section 4) --------------------------------------------------

def caps(*, day="1.00", month="0.20", first="0.50", ceiling=None, past="0.25") -> None:
    """The caps; the month's ceiling at the month unless given, so no soft brake between (the
    first paid month's ceiling is then its own allowance, since a ceiling is never below it)."""
    set_spend_limits(reader_usd_per_day=Decimal(day), reader_usd_per_month=Decimal(month),
                     platform_usd_per_day=100, first_paid_month_usd=Decimal(first),
                     reader_usd_month_ceiling=Decimal(ceiling or month),
                     reader_usd_per_day_past_month=Decimal(past))


def chat_at(dsn: str, owner: uuid.UUID, usd: str, when: str = "now()") -> None:
    """One settled chat row of ``usd`` at ``when`` (an SQL time), past row-level security."""
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO cost_ledger (owner_id, phase, model, usd, idempotency_key, purpose,"
            f" recorded_at) VALUES (%s, 'synthesis', 'm', %s, %s, 'stream-reply', {when})",
            (owner, Decimal(usd), f"companion:{uuid.uuid4().hex}"))


def first_paid(dsn: str, owner: uuid.UUID):
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT first_paid_at FROM reader_plans WHERE owner_id=%s",
                             (owner,)).fetchone()
    return None if found is None else found[0]


def first_month(dsn: str, owner: uuid.UUID):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT first_month_spent_usd FROM reader_plan_state(%s)",
                            (owner,)).fetchone()[0]


def move_first_paid(dsn: str, owner: uuid.UUID, when: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(f"UPDATE reader_plans SET first_paid_at = {when} WHERE owner_id=%s", (owner,))


def first_month_ends(dsn: str, owner: uuid.UUID) -> tuple[datetime, datetime]:
    """(when this reader's first paid month ends, the database's now), both in UTC."""
    with psycopg.connect(dsn) as conn:
        since, now = conn.execute("SELECT first_paid_at, now() FROM reader_plans"
                                  " WHERE owner_id=%s", (owner,)).fetchone()
    return (since + FIRST_PAID_MONTH).astimezone(UTC), now.astimezone(UTC)


def at_the_first_ceiling(dsn: str, owner: uuid.UUID, *, past_the_month: bool) -> str:
    """The line due at the first paid month's ceiling (see the two tests of it below): the
    month's line when the 30 days end in this calendar month and this month's chat has already
    reached the month's ceiling (``past_the_month``), else the line naming the day."""
    ends, now = first_month_ends(dsn, owner)
    if (ends.year, ends.month) == (now.year, now.month) and past_the_month:
        return month_wall(dsn)
    return first_month_line(back_on(ends))


def test_the_first_paid_month_holds_chat_since_the_plan_began_to_its_own_ceiling_once(
        paddle, tenant_dsn, database, readers):
    """The break it catches: a new subscriber held to the ordinary month in their first 30 days
    (or charged in it for what they spent before they paid); the larger month given again after a
    cancel and a new plan, or after the 30 days; the day guard dropped in it; a line other than
    today's month line at its ceiling; or an operator's own month cap for the reader ignored."""
    a = readers["a"]
    book = shelve(database, a)
    caps()
    chat_at(database, a, "0.15")                              # this month, before they paid
    now = datetime.now(UTC)
    post(paddle, message(sub_id("a1"), reader=a, event_type="subscription.created", at=now))
    stamped = first_paid(database, a)
    assert stamped is not None and first_month(database, a) == 0
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.30)                                   # past the $0.20 calendar month
    spend(chat, book, 0.19)
    assert first_month(database, a) == Decimal("0.49")
    spend(chat, book, 0.01)
    refused(chat, book, "month",                              # $0.50 since the plan began
            at_the_first_ceiling(database, a, past_the_month=True))
    caps(day="0.10", first="5")
    refused(chat, book, "day", _DAY)                          # the day is the day
    caps(first="5")
    update_limits(database, a, {"max_spend_usd_per_month": Decimal("0.60")})
    refused(chat, book, "month", month_wall(database))  # their own month cap wins
    update_limits(database, a, {"max_spend_usd_per_month": None})
    spend(chat, book, 0.01)

    # A cancel and a new plan: the first paid month is not had again, and its start stays.
    caps()
    post(paddle, message(sub_id("a1"), "canceled", reader=a, at=now + timedelta(seconds=1)))
    assert first_month(database, a) is None
    post(paddle, message(sub_id("a2"), reader=a, event_type="subscription.created",
                         at=now + timedelta(seconds=2)))
    assert first_paid(database, a) == stamped
    assert first_month(database, a) is not None               # still within its 30 days
    move_first_paid(database, a, "now() - interval '30 days 1 minute'")
    assert first_month(database, a) is None
    refused(chat, book, "month", month_wall(database))  # the calendar month: $0.66 > $0.20
    moved = first_paid(database, a)
    post(paddle, message(sub_id("a2"), "past_due", reader=a, at=now + timedelta(seconds=3)))
    assert first_paid(database, a) == moved


def test_the_first_paid_month_is_paddles_alone(paddle, tenant_dsn, database, readers):
    """The break it catches: a gifted reader given the first paid month (a gift has none), a
    gift over a new Paddle plan still given it, or a plan Paddle only lists as trialing (nothing
    paid yet) starting it."""
    a, b = readers["a"], readers["b"]
    book = shelve(database, a)
    caps()
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (a,))
    assert first_paid(database, a) is None and first_month(database, a) is None
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.20)
    refused(chat, book, "month", month_wall(database))
    post(paddle, message(sub_id("a"), reader=a))
    assert first_paid(database, a) is not None
    assert first_month(database, a) is None                   # the gift wins while it lasts
    refused(chat, book, "month", month_wall(database))
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, NULL::date)", (a,))
    assert first_month(database, a) == Decimal("0")
    post(paddle, message(sub_id("b"), "trialing", reader=b))
    assert plan_of(database, b) == ("paying", None, "paddle") and first_paid(database, b) is None


def test_the_first_paid_month_across_two_calendar_months_is_bounded(paddle, tenant_dsn,
                                                                    database, readers):
    """The bound, for a reader whose first 30 days straddle two calendar months: within those 30
    days their chat reaches at most the first paid month's ceiling, wherever the days fall; after
    them the calendar month (which counts what they spent in it, first month or not) holds them
    as it holds everyone. So the 30 days and the calendar month they end in together reach at
    most the first paid month plus one ordinary month, and no calendar month more than the first
    paid month. The break it catches: a rule that swaps the month's ceiling for the larger one
    for 30 days, which would allow it twice (once in each calendar month)."""
    if datetime.now(UTC).day > 28:
        pytest.skip("30 days from last month no longer reach today")
    a = readers["a"]
    book = shelve(database, a)
    caps()
    post(paddle, message(sub_id("a"), reader=a))
    month_start = "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
    move_first_paid(database, a, f"{month_start} - interval '1 day'")
    chat_at(database, a, "0.50", f"{month_start} - interval '12 hours'")   # all of it last month
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    assert first_month(database, a) == Decimal("0.50")
    refused(chat, book, "month",                              # nothing this month, still held
            at_the_first_ceiling(database, a, past_the_month=False))
    # The 30 days over: this calendar month alone, from nothing, to the ordinary ceiling.
    move_first_paid(database, a, f"{month_start} - interval '31 days'")
    spend(chat, book, 0.19)
    spend(chat, book, 0.01)
    refused(chat, book, "month", month_wall(database))
    # What was spent this month within the 30 days counts for this month after them.
    b = readers["b"]
    book_b = shelve(database, b)
    post(paddle, message(sub_id("b"), reader=b))
    move_first_paid(database, b, f"{month_start} - interval '1 day'")
    chat_b = HostedCompanionCatalog(tenant_dsn, OwnerId(b))
    spend(chat_b, book_b, 0.50)
    refused(chat_b, book_b, "month", at_the_first_ceiling(database, b, past_the_month=True))
    move_first_paid(database, b, f"{month_start} - interval '31 days'")
    refused(chat_b, book_b, "month", month_wall(database))  # $0.50 this month > $0.20


def test_at_the_first_ceiling_mothli_names_the_day_it_is_back_and_is_back_that_day(
        paddle, tenant_dsn, database, readers):
    """The break it catches: the line at the first paid month's ceiling promising a return it
    breaks. A reader who used their first month and whose 30 days end later than this month's
    chat allows them is told today's month line ("back at the start of the next") and is still
    stopped on the 1st. They are told the day instead: the first day that has begun nowhere on
    Earth before the 30 days end; still stopped a minute before the 30 days end; answered on the
    named day. The line names that day and nothing else: no amount, no count, no countdown."""
    a = readers["a"]
    book = shelve(database, a)
    caps(month="5", ceiling="0.50")                           # the ordinary month is not reached
    post(paddle, message(sub_id("a"), reader=a))
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    spend(chat, book, 0.50)
    ends, _now = first_month_ends(database, a)
    named = back_on(ends)
    line = first_month_line(named)
    refused(chat, book, "month", line)
    assert line == ("We’ve talked our way through your first month together already! I’ll be "
                    f"back on {named.day} {named:%B}. Your book and your place are right here.")
    assert named > ends.date() and "unlimited" not in line.casefold()
    assert "question" not in line.casefold() and "$" not in line
    assert [word for word in line.split() if any(c.isdigit() for c in word)] == [str(named.day)]

    # A minute before the 30 days end: still refused. (As if the clock stood then: the plan's
    # start moved back instead, so the day named is the one the moved end gives.)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reader_plans SET first_paid_at = first_paid_at"
                     " - (%s::timestamptz - now()) + interval '1 minute' WHERE owner_id=%s",
                     (ends, a))
    refused(chat, book, "month", first_month_line(back_on(first_month_ends(database, a)[0])))
    # The named day, where it begins first (UTC+14): the 30 days are over, and Mothli answers.
    # (As if the clock stood at that moment: the plan's start moved back by as much.)
    begins = datetime.combine(named, datetime.min.time(), UTC) - timedelta(hours=14)
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reader_plans SET first_paid_at = %s::timestamptz"
                     " - (%s::timestamptz - now()) WHERE owner_id=%s",
                     (ends - FIRST_PAID_MONTH, begins, a))
    assert begins >= ends
    assert first_month(database, a) is None
    spend(chat, book, 0.01)


def test_at_the_first_ceiling_mothli_says_the_1st_when_the_1st_is_when_it_is_back(
        paddle, tenant_dsn, database, readers):
    """The other case: the 30 days end within this calendar month and this month's chat has
    already reached the ordinary month, so the reader can talk again on the 1st, which today's
    month line says; with the ordinary month not reached, the line names the day the 30 days end.
    The break it catches: either line shown where the other is the true one."""
    a = readers["a"]
    book = shelve(database, a)
    caps()                                                    # ordinary month $0.20, first $0.50
    post(paddle, message(sub_id("a"), reader=a))
    with psycopg.connect(database) as conn:
        soon = conn.execute("SELECT date_trunc('month', (now() + interval '1 hour') AT TIME ZONE"
                            " 'UTC') = date_trunc('month', now() AT TIME ZONE 'UTC')").fetchone()[0]
    if not soon:
        pytest.skip("the 30 days would end in the next calendar month within the hour")
    move_first_paid(database, a, "now() - interval '30 days' + interval '1 hour'")
    chat_at(database, a, "0.50")                              # this month, within the 30 days
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    assert first_month(database, a) == Decimal("0.50")
    refused(chat, book, "month", month_wall(database))  # $0.50 this month > $0.20
    caps(month="5", ceiling="0.50")
    ends, _now = first_month_ends(database, a)
    refused(chat, book, "month", first_month_line(back_on(ends)))


# ---- the soft brake (the owner, 27 November 2026) ---------------------------------------------

MONTH_START = "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


def this_month(dsn: str, owner: uuid.UUID) -> Decimal:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            f"SELECT COALESCE(sum(usd), 0) FROM cost_ledger WHERE owner_id=%s"
            f" AND recorded_at >= {MONTH_START}", (owner,)).fetchone()[0]


def test_past_the_month_a_paying_reader_keeps_mothli_every_day_until_the_ceiling(
        paddle, tenant_dsn, database, readers):
    """The soft brake, in the calendar month. Below the month allowance nothing changes; past
    it, where a reader was stopped until the 1st, they are answered up to the smaller day and then
    told to pick up tomorrow; at the month's ceiling they are stopped as before, told the day
    Mothli is back. An operator's own caps still win: their day is the reader's day past the
    allowance too, and their month stops the reader with no brake. The break it catches: the
    brake missing (the old wall), braking a reader below the allowance, a ceiling that does not
    stop, or a line other than the one due."""
    with psycopg.connect(database) as conn:
        first_day = conn.execute(
            "SELECT extract(day FROM now() AT TIME ZONE 'UTC') = 1").fetchone()[0]
    if first_day:
        pytest.skip("no earlier day this month to have talked on")
    a = readers["a"]
    book = shelve(database, a)
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (a,))
    caps(day="0.10", month="0.20", ceiling="0.40", past="0.03")
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    chat_at(database, a, "0.05", MONTH_START)                  # an earlier day this month
    spend(chat, book, 0.05)
    spend(chat, book, 0.05)                                   # /usr/bin/bash.15 this month, /usr/bin/bash.10 today
    refused(chat, book, "day", _DAY)                           # below the allowance: as before
    caps(day="1.00", month="0.20", ceiling="0.40", past="0.15")
    spend(chat, book, 0.05)                                   # /usr/bin/bash.20: the month allowance
    refused(chat, book, "day", _DAY_PAST_MONTH)                # /usr/bin/bash.15 today: the smaller day
    caps(day="1.00", month="0.20", ceiling="0.40", past="0.20")
    spend(chat, book, 0.01)                                   # before: stopped at the month
    update_limits(database, a, {"max_spend_usd_per_day": Decimal("0.50")})
    spend(chat, book, 0.05)                                   # their own day, past the allowance
    update_limits(database, a, {"max_spend_usd_per_day": None,
                                "max_spend_usd_per_month": Decimal("0.25")})
    refused(chat, book, "month", month_wall(database))         # their own month: no brake
    update_limits(database, a, {"max_spend_usd_per_month": None})
    assert this_month(database, a) == Decimal("0.26")
    caps(day="1.00", month="0.20", ceiling="0.40", past="1.00")
    spend(chat, book, 0.14)
    refused(chat, book, "month", month_wall(database))         # the ceiling (/usr/bin/bash.40), as before
    caps(day="1.00", month="0.45", ceiling="0.10")           # a ceiling below the month is
    spend(chat, book, 0.05)                                   # the month
    refused(chat, book, "month", month_wall(database))


def test_past_the_first_paid_month_a_subscriber_keeps_mothli_every_day_until_its_ceiling(
        paddle, tenant_dsn, database, readers):
    """The soft brake in a subscriber's first paid month, measured as that month is: their chat
    since the plan began. Past its allowance, where they were stopped until the 30 days ended,
    they are answered up to the smaller day; at the ceiling they are stopped, told the day
    Mothli is back. The break it catches: the first paid month's allowance still a wall, or its
    ceiling measured on the calendar month."""
    a = readers["a"]
    book = shelve(database, a)
    caps(day="1.00", month="0.20", first="0.50", ceiling="0.60", past="0.03")
    post(paddle, message(sub_id("a"), reader=a))
    move_first_paid(database, a, "now() - interval '2 days'")
    chat_at(database, a, "0.50", "now() - interval '1 day'")   # the first month's allowance
    chat = HostedCompanionCatalog(tenant_dsn, OwnerId(a))
    assert first_month(database, a) == Decimal("0.50")
    spend(chat, book, 0.02)                                   # before: stopped
    spend(chat, book, 0.01)
    refused(chat, book, "day", _DAY_PAST_MONTH)
    chat_at(database, a, "0.07", "now() - interval '1 day'")
    assert first_month(database, a) == Decimal("0.60")
    refused(chat, book, "month", at_the_first_ceiling(
        database, a, past_the_month=this_month(database, a) >= Decimal("0.60")))
