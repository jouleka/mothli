"""Subscribing, cancelling, keeping a plan and the customer portal through Paddle (ADR 0045 step 4,
the server half), and the regular check of plans against Paddle, on real PostgreSQL.

Through the hosted HTTP app, signed in with the CSRF token, the billing role granted exactly what
the deployment's role script grants it. Paddle is a stand-in behind the real client
(``httpx.MockTransport``): nothing calls Paddle or any other outside service, and no model is
called. Paddle's account is shared with another product of the owner's (MySigner here): the
stand-in holds a MySigner subscription and customer, and every request the server sends is
checked to be about an id Mothli stored for the signed-in reader, or a transaction with one of
Mothli's prices.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest

from app.hosted.auth.api import CSRF_HEADER
from app.hosted.paddle_api import PaddleApi
from app.hosted.paddle_check import check
from tests.hosted import test_paddle_messages_postgres as messages
from tests.hosted.test_companion_stream_postgres import (
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
)
from tests.hosted.test_paddle_messages_postgres import (
    MONTHLY,
    MYSIGNER,
    PADDLE,
    YEARLY,
    YEARLY_NEW,
    message,
    paddle_id,
    post,
    stamp_of,
    sub_id,
)
from tests.hosted.test_tenant_postgres import _become

pytestmark = pytest.mark.postgres

admin_dsn = messages.admin_dsn
database = messages.database
tenant_dsn = messages.tenant_dsn
readers = messages.readers
make_app = messages.make_app
billing_dsn = messages.billing_dsn

# A stand-in in the shape of a sandbox API key, built in pieces: not a key.
KEY = "_".join(("pdl", "sdbx", "apikey", "teststandin" * 4))
PRICES = frozenset({MONTHLY, YEARLY, YEARLY_NEW})
MYSIGNER_SUB = paddle_id("sub_", "mysignerplan")
MYSIGNER_CUSTOMER = paddle_id("ctm_", "mysignercustomer")
PORTAL_TOKEN = "portaltokenstandin" * 2


class StandInPaddle:
    """Paddle's API as far as Mothli uses it, in memory. ``fail`` makes every call answer as a
    slow (``timeout``), unreachable (``down``) or overloaded (``busy``) Paddle, or refuse with
    an error code (``("refuse", status, code)``)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object, dict]] = []
        self.subscriptions: dict[str, dict] = {}
        self.transactions: dict[str, dict] = {}
        self.fail = None
        self.clock = datetime.now(UTC) + timedelta(minutes=1)
        # The owner's other product, in the same account.
        self.subscriptions[MYSIGNER_SUB] = self.entity(MYSIGNER_SUB, MYSIGNER_CUSTOMER,
                                                       price=MYSIGNER)

    def tick(self) -> str:
        self.clock += timedelta(minutes=1)
        return stamp_of(self.clock)

    def entity(self, sub: str, customer: str, *, price: str = MONTHLY, custom=None,
               status: str = "active") -> dict:
        data = message(sub, status, prices=(price,), customer=customer, custom=custom or {},
                       at=self.clock)["data"]
        data["updated_at"] = self.tick()
        return data

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body, dict(request.headers)))
        if self.fail == "timeout":
            raise httpx.ReadTimeout("stand-in: slow", request=request)
        if self.fail == "down":
            raise httpx.ConnectError("stand-in: unreachable", request=request)
        if self.fail == "busy":
            return httpx.Response(429, json={"error": {"type": "api_error",
                                                       "code": "too_many_requests"}})
        if isinstance(self.fail, tuple):
            _refuse, status, code = self.fail
            return httpx.Response(status, json={"error": {"type": "request_error", "code": code,
                                                          "detail": "stand-in refusal"}})
        parts = request.url.path.strip("/").split("/")
        if request.method == "POST" and parts == ["transactions"]:
            transaction = paddle_id("txn_", uuid.uuid4().hex)
            self.transactions[transaction] = body
            return httpx.Response(201, json={"data": {"id": transaction, "status": "draft",
                                                      "custom_data": body["custom_data"]}})
        if parts[0] == "subscriptions" and parts[1] in self.subscriptions:
            entity = self.subscriptions[parts[1]]
            if request.method == "POST" and parts[2:] == ["cancel"]:
                entity["scheduled_change"] = {
                    "action": "cancel", "resume_at": None,
                    "effective_at": entity["current_billing_period"]["ends_at"]}
                entity["updated_at"] = self.tick()
            elif request.method == "PATCH" and len(parts) == 2:
                entity["scheduled_change"] = body["scheduled_change"]
                entity["updated_at"] = self.tick()
            elif not (request.method == "GET" and len(parts) == 2):
                return httpx.Response(404)
            return httpx.Response(200, json={"data": entity})
        if request.method == "POST" and parts[0] == "customers" and parts[2:] == [
                "portal-sessions"]:
            base = f"https://customer-portal.paddle.com/cpl_standin?token={PORTAL_TOKEN}"
            return httpx.Response(201, json={"data": {
                "id": "cpls_standin", "customer_id": parts[1],
                "urls": {"general": {"overview": base},
                         "subscriptions": [{
                             "id": sub, "view_subscription": f"{base}&view={sub}",
                             "cancel_subscription": f"{base}&cancel={sub}",
                             "update_subscription_payment_method": f"{base}&card={sub}"}
                             for sub in body["subscription_ids"]]}}})
        return httpx.Response(404, json={"error": {"type": "request_error", "code": "not_found"}})


@pytest.fixture()
def shop(make_app, billing_dsn, database):
    """The hosted app with Paddle's messages and API on, Paddle stood in: (client, tokens,
    stand-in). A message ``post`` sends naming a reader comes with that reader's checkout."""
    client, tokens, app = make_app(**PADDLE, paddle_api_key=KEY, hosted_billing_dsn=billing_dsn)
    standin = StandInPaddle()
    app.state.paddle_api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    client.checkouts_dsn = database
    return client, tokens, standin


def ask(client, tokens, label: str, route: str, body: dict | None = None):
    csrf = _become(client, tokens, label)
    return client.post(f"/api/billing/{route}", json=body, headers={CSRF_HEADER: csrf})


def plan(client, tokens, label: str) -> dict:
    _become(client, tokens, label)
    response = client.get("/api/billing/plan")
    assert response.status_code == 200, response.text
    return response.json()


def subscribe(client, tokens, standin: StandInPaddle, label: str, owner, sub: str, *,
              which: str = "monthly", status: str = "active") -> dict:
    """The whole way in: the reader's checkout, then Paddle's message about the subscription
    that payment made, carrying the transaction's custom data as Paddle copies it."""
    response = ask(client, tokens, label, "checkout", {"plan": which})
    assert response.status_code == 200, response.text
    custom = standin.transactions[response.json()["transaction_id"]]["custom_data"]
    price = MONTHLY if which == "monthly" else YEARLY_NEW
    customer = paddle_id("ctm_", f"c{str(owner)[:8]}")
    entity = standin.entity(sub, customer, price=price, custom=custom, status=status)
    standin.subscriptions[sub] = entity
    payload = message(sub, status, prices=(price,), customer=customer, custom=custom,
                      event_type="subscription.created", at=datetime.now(UTC))
    payload["data"] = json.loads(json.dumps(entity))           # the subscription Paddle holds
    assert post(client, payload, seed=False).status_code == 200
    return custom


def row(dsn: str, owner) -> dict | None:
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT to_jsonb(plan) - 'updated_at' FROM reader_plans AS plan"
                             " WHERE owner_id=%s", (owner,)).fetchone()
    return None if found is None else found[0]


def checkouts(dsn: str, owner=None) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT owner_id, token_digest, used_at IS NOT NULL,"
            " round(extract(epoch FROM expires_at - created_at) / 86400)"
            " FROM paddle_checkouts WHERE %s::uuid IS NULL OR owner_id = %s ORDER BY created_at",
            (owner, owner)).fetchall()


def written(dsn: str) -> tuple:
    """Everything the billing role may write: what a refused or failed request must leave."""
    with psycopg.connect(dsn) as conn:
        return tuple(conn.execute(f"SELECT to_jsonb(t) FROM {table} AS t ORDER BY to_jsonb(t)::text"
                                  ).fetchall()
                     for table in ("reader_plans", "paddle_events", "paddle_checkouts"))


def allowed(standin: StandInPaddle, stored: dict[str, str]) -> None:
    """The shared-account rule: every request is a transaction with one of Mothli's prices, or
    about a subscription or customer Mothli stored (``stored``: subscription -> customer)."""
    customers = set(stored.values())
    for method, path, body, headers in standin.requests:
        parts = path.strip("/").split("/")
        assert headers["authorization"] == f"Bearer {KEY}"
        assert headers["paddle-version"] == "1"
        if parts == ["transactions"]:
            assert method == "POST"
            assert [item["price_id"] for item in body["items"]] in ([MONTHLY], [YEARLY_NEW])
        elif parts[0] == "subscriptions":
            assert parts[1] in stored, path
            assert (method, parts[2:]) in (("GET", []), ("POST", ["cancel"]), ("PATCH", []))
        else:
            assert parts[0] == "customers" and parts[2:] == ["portal-sessions"], path
            assert method == "POST" and parts[1] in customers
            assert body == {"subscription_ids": [s for s, c in stored.items() if c == parts[1]]}
        assert MYSIGNER_SUB not in path and MYSIGNER_CUSTOMER not in path


# ---- checkout and the binding ------------------------------------------------------------------

def test_a_checkout_is_the_server_made_transaction_and_it_alone_makes_its_reader_paying(
        shop, database, readers):
    """The break it catches: the transaction made for another price than the plan's current one
    (the first id of its setting), without the reader and a fresh checkout token in its custom
    data, or answered with more than the browser needs; the token kept in clear, not for seven
    days, or not used up by the subscription it made; a reader made paying by anything but a
    message about a subscription that came from their own checkout."""
    client, tokens, standin = shop
    a = readers["a"]
    response = ask(client, tokens, "a", "checkout", {"plan": "yearly"})
    assert response.status_code == 200
    assert set(response.json()) == {"transaction_id"}
    assert response.headers["cache-control"].startswith(("no-store", "private, no-store"))
    [(method, path, body, headers)] = standin.requests
    assert (method, path) == ("POST", "/transactions")
    assert body["items"] == [{"price_id": YEARLY_NEW, "quantity": 1}]
    assert set(body) == {"items", "custom_data"}               # no customer looked up or set
    token = body["custom_data"]["mothli_checkout"]
    assert body["custom_data"] == {"mothli_reader": str(a), "mothli_checkout": token}
    assert len(token) == 43
    [(owner, digest, used, days)] = checkouts(database, a)
    assert (owner, used, days) == (a, False, 7)
    assert bytes(digest) != token.encode() and len(bytes(digest)) == 32
    assert messages.plan_of(database, a)[0] == "new"           # the checkout alone: nothing

    # Paddle's message about the subscription that payment made: paying, the token used.
    standin.requests.clear()
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    assert messages.plan_of(database, a) == ("paying", None, "paddle")
    assert [used for _o, _d, used, _days in checkouts(database, a)] == [False, True]
    assert row(database, a)["paddle_price_id"] == MONTHLY
    assert plan(client, tokens, "a")["plan"] == "monthly"
    assert standin.requests[0][2]["items"] == [{"price_id": MONTHLY, "quantity": 1}]


def test_a_checkout_is_refused_while_the_reader_has_a_live_plan_and_a_gift_does_not_stop_it(
        shop, database, readers):
    """The break it catches: a reader who already pays charged twice by a second subscription
    (refused, and Paddle never asked), including one whose plan is cancelled to the end of the
    period; a reader whose plan ended unable to come back; a gifted reader unable to subscribe
    (the gift still wins while it lasts)."""
    client, tokens, standin = shop
    a, b = readers["a"], readers["b"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    standin.requests.clear()
    for which in ("monthly", "yearly"):
        response = ask(client, tokens, "a", "checkout", {"plan": which})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "already_subscribed"
    assert ask(client, tokens, "a", "cancel").status_code == 200
    standin.requests.clear()
    before = written(database)
    assert ask(client, tokens, "a", "checkout", {"plan": "monthly"}).status_code == 409
    assert standin.requests == [] and written(database) == before
    # Ended: subscribing again is a new checkout.
    post(client, message(sub_id("a"), "canceled", customer=paddle_id("ctm_", f"c{str(a)[:8]}"),
                         custom={}, at=datetime.now(UTC) + timedelta(hours=1)), seed=False)
    assert messages.plan_of(database, a) == ("ended", "plan", None)
    assert ask(client, tokens, "a", "checkout", {"plan": "monthly"}).status_code == 200
    # A gift: may subscribe; the gift wins while it lasts.
    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '9999-12-31'::date)", (b,))
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    assert messages.plan_of(database, b) == ("paying", None, "gift")
    seen = plan(client, tokens, "b")
    assert (seen["plan"], seen["subscribed"], seen["next_date"]) == ("gift", True, None)


def test_checkout_refusals_answer_and_write_nothing(shop, make_app, billing_dsn, database,
                                                    readers):
    """The break it catches: a checkout without a session or the CSRF token, for a plan that is
    not Mothli's, naming an account, or past the day's few checkouts, reaching Paddle or writing
    a row; a plan with no price set offered anyway."""
    client, tokens, standin = shop
    before = written(database)
    client.cookies.clear()
    assert client.post("/api/billing/checkout", json={"plan": "monthly"}).status_code == 401
    _become(client, tokens, "a")
    assert client.post("/api/billing/checkout", json={"plan": "monthly"}).status_code == 403
    for body in ({"plan": "weekly"}, {}, {"plan": "monthly", "owner_id": str(readers["b"])},
                 {"plan": "monthly", "price_id": MYSIGNER}):
        assert ask(client, tokens, "a", "checkout", body).status_code == 422, body
    csrf = _become(client, tokens, "a")
    assert client.post(f"/api/billing/checkout?owner_id={readers['b']}", json={"plan": "monthly"},
                       headers={CSRF_HEADER: csrf}).status_code == 422
    assert standin.requests == [] and written(database) == before

    for _ in range(10):
        assert ask(client, tokens, "a", "checkout", {"plan": "monthly"}).status_code == 200
    calls = len(standin.requests)
    response = ask(client, tokens, "a", "checkout", {"plan": "monthly"})
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "checkout_busy"
    assert len(standin.requests) == calls and len(checkouts(database, readers["a"])) == 10
    # B is not held by A's checkouts.
    assert ask(client, tokens, "b", "checkout", {"plan": "monthly"}).status_code == 200

    other, other_tokens, app = make_app(**{**PADDLE, "paddle_price_yearly": ""},
                                        paddle_api_key=KEY, hosted_billing_dsn=billing_dsn)
    lonely = StandInPaddle()
    app.state.paddle_api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(lonely))
    response = ask(other, other_tokens, "b", "checkout", {"plan": "yearly"})
    assert (response.status_code, response.json()["detail"]["code"]) == (404, "plan_not_sold")
    assert lonely.requests == []


def test_without_the_api_key_the_paddle_routes_do_not_exist(make_app, billing_dsn, readers):
    """The break it catches: a server with no PADDLE_API_KEY offering checkout, cancel, keep or
    the portal. The plan is still readable (a gift needs no Paddle)."""
    client, tokens, _app = make_app(**PADDLE, hosted_billing_dsn=billing_dsn)
    for route in ("checkout", "cancel", "keep", "portal"):
        assert ask(client, tokens, "a", route, {"plan": "monthly"}).status_code in (404, 405)
    assert plan(client, tokens, "a")["state"] == "new"


# ---- Paddle down, slow or refusing ---------------------------------------------------------------

def test_paddle_down_slow_or_overloaded_gives_a_kind_error_and_writes_nothing(shop, database,
                                                                             readers, caplog):
    """The break it catches: a reader shown a crash, a raw error or Paddle's words when Paddle
    is unreachable, slow, overloaded or refusing; a checkout row or a plan change written though
    Paddle did nothing; or a request sent twice (a retry could charge or cancel twice)."""
    client, tokens, standin = shop
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    caplog.set_level(logging.DEBUG)
    before = written(database)
    for fail in ("timeout", "down", "busy", ("refuse", 400, "bad_request"),
                 ("refuse", 403, "forbidden")):
        standin.fail = fail
        for route, body in (("checkout", {"plan": "monthly"}), ("cancel", None),
                            ("portal", None)):
            if route == "checkout":
                # Only a reader without a live plan gets as far as Paddle.
                response = ask(client, tokens, "b", route, body)
            else:
                response = ask(client, tokens, "a", route, body)
            assert response.status_code == 503, (fail, route)
            detail = response.json()["detail"]
            assert detail["code"] == "paddle_unavailable"
            assert "nothing has changed" in detail["message"]
            assert not any(character.isdigit() for character in detail["message"])
            assert "paddle" not in detail["message"].casefold()
    assert written(database) == before
    assert checkouts(database, readers["b"]) == []
    # Each call was sent once.
    assert len(standin.requests) == 1 + 5 * 3
    assert "stand-in" not in caplog.text


def test_a_cancel_within_thirty_minutes_of_the_renewal_says_try_again_in_a_few_minutes(
        shop, database, readers):
    """The break it catches: Paddle refusing a cancel because the plan renews within 30 minutes
    (``subscription_locked_renewal``, 409) shown as a failure, or as anything but "try again in a
    few minutes", or a change written."""
    client, tokens, standin = shop
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    before = written(database)
    for code in ("subscription_locked_renewal", "subscription_locked_processing"):
        standin.fail = ("refuse", 409, code)
        response = ask(client, tokens, "a", "cancel")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "try_again_soon"
        assert "few minutes" in detail["message"]
        assert not any(character.isdigit() for character in detail["message"])
    assert written(database) == before
    standin.fail = None
    assert ask(client, tokens, "a", "cancel").json()["cancel_scheduled"] is True


# ---- cancel and keep ------------------------------------------------------------------------------

def test_cancel_schedules_the_end_of_the_period_and_keep_removes_it(shop, database, readers):
    """The break it catches: a cancel that ends the plan at once, or is not asked of Paddle as
    ``effective_from: next_billing_period``; the reader not paying to the end of what they paid
    for; keep not removing the scheduled change; either sent twice when already as asked; the
    answer not the plan as the account sheet reads it."""
    client, tokens, standin = shop
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    period_end = standin.subscriptions[sub_id("a")]["current_billing_period"]["ends_at"]
    standin.requests.clear()
    response = ask(client, tokens, "a", "cancel")
    assert response.status_code == 200
    answer = response.json()
    assert [(m, p, b) for m, p, b, _h in standin.requests] == [
        ("POST", f"/subscriptions/{sub_id('a')}/cancel",
         {"effective_from": "next_billing_period"})]
    assert (answer["state"], answer["plan"], answer["subscribed"],
            answer["cancel_scheduled"]) == ("paying", "monthly", True, True)
    assert datetime.fromisoformat(answer["next_date"]) == datetime.fromisoformat(
        period_end.replace("Z", "+00:00"))
    assert messages.plan_of(database, a) == ("paying", None, "paddle")
    assert row(database, a)["ends_at"] is not None
    assert plan(client, tokens, "a") == answer
    # Already cancelled: nothing sent.
    assert ask(client, tokens, "a", "cancel").json() == answer
    assert len(standin.requests) == 1

    response = ask(client, tokens, "a", "keep")
    assert response.status_code == 200
    assert standin.requests[-1][:3] == ("PATCH", f"/subscriptions/{sub_id('a')}",
                                        {"scheduled_change": None})
    kept = response.json()
    assert (kept["cancel_scheduled"], kept["subscribed"], kept["plan"]) == (False, True,
                                                                            "monthly")
    assert row(database, a)["ends_at"] is None
    assert ask(client, tokens, "a", "keep").json() == kept
    assert len(standin.requests) == 2
    assert plan(client, tokens, "a") == kept


def test_cancel_keep_and_portal_need_a_plan_of_ones_own(shop, database, readers):
    """The break it catches: a reader with no plan, or an ended one, reaching Paddle; or any of
    the routes without a session or the CSRF token."""
    client, tokens, standin = shop
    for route in ("cancel", "keep", "portal"):
        response = ask(client, tokens, "a", route)
        assert (response.status_code, response.json()["detail"]["code"]) == (409, "no_plan")
        client.cookies.clear()
        assert client.post(f"/api/billing/{route}").status_code == 401
        _become(client, tokens, "a")
        assert client.post(f"/api/billing/{route}").status_code == 403
    client.cookies.clear()
    assert client.get("/api/billing/plan").status_code == 401
    assert standin.requests == []


# ---- the customer portal ------------------------------------------------------------------------

def test_the_portal_link_is_for_the_readers_own_plan_and_is_neither_stored_nor_logged(
        shop, database, readers, caplog):
    """The break it catches: a portal session made for another customer or subscription than
    the reader's stored ones; the link (it carries a temporary token) written to the database,
    a log line or a cache; or a link that is not Paddle's https page."""
    client, tokens, standin = shop
    a = readers["a"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    customer = row(database, a)["paddle_customer_id"]
    caplog.set_level(logging.DEBUG)
    standin.requests.clear()
    response = ask(client, tokens, "a", "portal")
    assert response.status_code == 200
    assert set(response.json()) == {"url"}
    link = response.json()["url"]
    assert link.startswith("https://customer-portal.paddle.com/") and f"card={sub_id('a')}" in link
    assert "no-store" in response.headers["cache-control"]
    assert [(m, p, b) for m, p, b, _h in standin.requests] == [
        ("POST", f"/customers/{customer}/portal-sessions", {"subscription_ids": [sub_id("a")]})]
    assert PORTAL_TOKEN not in caplog.text
    with psycopg.connect(database) as conn:
        for table in ("reader_plans", "paddle_events", "paddle_checkouts", "audit_events"):
            dump = conn.execute(f"SELECT COALESCE(string_agg(to_jsonb(t)::text, ''), '')"
                                f" FROM {table} AS t").fetchone()[0]
            assert PORTAL_TOKEN not in dump, table
    # An ended plan: the overview, for receipts.
    post(client, message(sub_id("a"), "canceled", customer=customer, custom={},
                         at=datetime.now(UTC) + timedelta(hours=1)), seed=False)
    link = ask(client, tokens, "a", "portal").json()["url"]
    assert "card=" not in link and link.startswith("https://customer-portal.paddle.com/")


# ---- one reader, one plan -------------------------------------------------------------------------

def test_reader_a_can_neither_act_on_nor_learn_about_reader_bs_plan(shop, database, readers):
    """The break it catches: reader A's session cancelling, keeping, opening the portal for or
    reading reader B's plan, by any route or by naming B's subscription, customer or account in
    the body or the address. Every route acts on the session's reader alone."""
    client, tokens, standin = shop
    b = readers["b"]
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    b_row, b_plan = row(database, b), plan(client, tokens, "b")
    customer = b_row["paddle_customer_id"]
    standin.requests.clear()
    named = {"subscription_id": sub_id("b"), "customer_id": customer, "owner_id": str(b)}
    for route in ("cancel", "keep", "portal"):
        for body in (None, named):
            response = ask(client, tokens, "a", route, body)
            assert (response.status_code, response.json()["detail"]["code"]) == (409, "no_plan")
            assert sub_id("b") not in response.text and customer not in response.text
    seen = plan(client, tokens, "a")
    assert (seen["state"], seen["plan"], seen["subscribed"], seen["next_date"]) == (
        "new", None, False, None)
    _become(client, tokens, "a")
    assert client.get(f"/api/billing/plan?owner_id={b}").status_code == 422
    assert standin.requests == []
    assert row(database, b) == b_row and plan(client, tokens, "b") == b_plan


# ---- what the account sheet reads -----------------------------------------------------------------

def test_the_plan_says_state_kind_date_cancel_and_retry_and_never_an_amount(shop, database,
                                                                           readers, tenant_dsn):
    """The break it catches: the account sheet told an amount, a count of questions or a
    countdown; the wrong kind of plan; the wrong date (the free weeks' end, the renewal, the
    scheduled end, the gift's last day); a payment Paddle is retrying not said."""
    client, tokens, standin = shop
    a, b = readers["a"], readers["b"]
    keys = {"state", "why", "plan", "next_date", "subscribed", "cancel_scheduled",
            "payment_retrying"}
    new = plan(client, tokens, "a")
    assert new == {"state": "new", "why": None, "plan": None, "next_date": None,
                   "subscribed": False, "cancel_scheduled": False, "payment_retrying": False}
    messages.stamp(tenant_dsn, a)
    free = plan(client, tokens, "a")
    assert (free["state"], free["plan"]) == ("free", None)
    with psycopg.connect(database) as conn:
        ends = conn.execute("SELECT free_ends_at FROM reader_plan_state(%s, 14, 2.10)",
                            (a,)).fetchone()[0]
    assert datetime.fromisoformat(free["next_date"]) == ends

    subscribe(client, tokens, standin, "a", a, sub_id("a"), which="yearly")
    paying = plan(client, tokens, "a")
    assert (paying["state"], paying["plan"], paying["subscribed"]) == ("paying", "yearly", True)
    renews = standin.subscriptions[sub_id("a")]["current_billing_period"]["ends_at"]
    assert datetime.fromisoformat(paying["next_date"]) == datetime.fromisoformat(
        renews.replace("Z", "+00:00"))
    post(client, message(sub_id("a"), "past_due", prices=(YEARLY_NEW,), custom={},
                         customer=row(database, a)["paddle_customer_id"],
                         at=datetime.now(UTC) + timedelta(hours=1)), seed=False)
    retrying = plan(client, tokens, "a")
    assert (retrying["state"], retrying["payment_retrying"]) == ("paying", True)

    with psycopg.connect(database) as conn:
        conn.execute("SELECT set_gift(%s, '2099-06-30'::date)", (b,))
    gift = plan(client, tokens, "b")
    assert (gift["plan"], gift["subscribed"]) == ("gift", False)
    assert gift["next_date"].startswith("2099-06-30T23:59:59")
    for seen in (new, free, paying, retrying, gift):
        assert set(seen) == keys
        text = json.dumps(seen).casefold()
        for word in ("usd", "spent", "question", "left", "unlimited", "remaining", "$"):
            assert word not in text, word


# ---- the shared Paddle account ----------------------------------------------------------------

def test_every_paddle_call_is_about_mothlis_price_or_an_id_stored_for_the_reader(
        shop, billing_dsn, database, readers):
    """The break it catches: any call that could reach the owner's other product in the shared
    Paddle account (a list, a search, a customer made or looked up by email, a subscription or a
    customer Mothli did not store for the signed-in reader). After every route and the regular
    check, each request the server sent is a transaction with one of Mothli's prices or about the
    reader's own stored subscription or customer; MySigner's are never named."""
    client, tokens, standin = shop
    a, b = readers["a"], readers["b"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    subscribe(client, tokens, standin, "b", b, sub_id("b"), which="yearly")
    for label in ("a", "b"):
        for route in ("cancel", "keep", "portal"):
            assert ask(client, tokens, label, route).status_code == 200, (label, route)
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    assert check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None)[0] == 2
    stored = {sub_id("a"): row(database, a)["paddle_customer_id"],
              sub_id("b"): row(database, b)["paddle_customer_id"]}
    allowed(standin, stored)
    assert {path for _m, path, _b, _h in standin.requests} >= {
        "/transactions", f"/subscriptions/{sub_id('a')}", f"/subscriptions/{sub_id('b')}"}


# ---- the regular check ----------------------------------------------------------------------------

def test_the_regular_check_applies_a_changed_plan_and_fetches_only_stored_ids(
        shop, billing_dsn, database, readers):
    """The break it catches: a lost message left unrepaired (a plan Paddle moved to past_due or
    cancelled, still shown as before); the check fetching an id Mothli did not store, a plan
    long ended, or an erased reader's; applying an older state over a newer one, or a
    subscription whose price is not Mothli's; no pause between calls; carrying on against a
    Paddle that is down."""
    client, tokens, standin = shop
    a, b = readers["a"], readers["b"]
    subscribe(client, tokens, standin, "a", a, sub_id("a"))
    subscribe(client, tokens, standin, "b", b, sub_id("b"))
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    pauses: list[float] = []

    # A change whose message never came: Paddle has A past due.
    standin.subscriptions[sub_id("a")]["status"] = "past_due"
    standin.subscriptions[sub_id("a")]["updated_at"] = standin.tick()
    # B's answer is older than what was applied: not applied.
    b_before = row(database, b)
    standin.subscriptions[sub_id("b")]["status"] = "canceled"
    standin.subscriptions[sub_id("b")]["updated_at"] = stamp_of(
        datetime.now(UTC) - timedelta(days=1))
    standin.requests.clear()
    assert check(billing_dsn, api, PRICES, size=50, pause=0.5, sleep=pauses.append) == (2, 1, 0)
    assert row(database, a)["paddle_status"] == "past_due"
    assert row(database, b) == b_before
    assert pauses == [0.5]
    assert sorted(path for _m, path, _b, _h in standin.requests) == sorted(
        [f"/subscriptions/{sub_id('a')}", f"/subscriptions/{sub_id('b')}"])
    assert all(method == "GET" for method, _p, _b, _h in standin.requests)

    # A stored id whose answer is about another product's price: never applied.
    standin.subscriptions[sub_id("a")]["items"][0]["price"]["id"] = MYSIGNER
    standin.subscriptions[sub_id("a")]["status"] = "canceled"
    standin.subscriptions[sub_id("a")]["updated_at"] = standin.tick()
    a_before = row(database, a)
    check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None)
    assert row(database, a) == a_before

    # Ended long ago, or erased: not checked at all.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reader_plans SET paddle_status = 'canceled', ends_at = NULL,"
                     " paddle_occurred_at = now() - interval '40 days' WHERE owner_id = %s", (a,))
        conn.execute("UPDATE users SET status = 'deleted', deleted_at = now() WHERE id = %s",
                     (b,))
    standin.requests.clear()
    assert check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None) == (0, 0, 0)
    assert standin.requests == []

    # Paddle down: one try, then the run stops; nothing written.
    with psycopg.connect(database) as conn:
        conn.execute("UPDATE reader_plans SET paddle_occurred_at = now() WHERE owner_id = %s",
                     (a,))
    before = written(database)
    standin.fail = "down"
    standin.requests.clear()
    assert check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None) == (0, 0, 1)
    assert len(standin.requests) == 1 and written(database) == before


def test_the_regular_check_takes_a_page_at_a_time(shop, billing_dsn, database, readers):
    """The break it catches: a run that fetches every stored plan at once, however many."""
    client, tokens, standin = shop
    subscribe(client, tokens, standin, "a", readers["a"], sub_id("a"))
    subscribe(client, tokens, standin, "b", readers["b"], sub_id("b"))
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    standin.requests.clear()
    assert check(billing_dsn, api, PRICES, size=1, pause=0, sleep=lambda _s: None)[0] == 1
    assert len(standin.requests) == 1


# ---- the logs --------------------------------------------------------------------------------------

def test_no_log_line_holds_the_key_a_token_an_id_or_a_payload(shop, billing_dsn, database,
                                                            readers, caplog):
    """The break it catches: Paddle's API key, a checkout token, a transaction, subscription or
    customer id, the portal link or any request or answer body written to the logs, on success
    or on failure."""
    client, tokens, standin = shop
    a = readers["a"]
    caplog.set_level(logging.DEBUG)
    custom = subscribe(client, tokens, standin, "a", a, sub_id("a"))
    for route in ("cancel", "keep", "portal"):
        ask(client, tokens, "a", route)
    transaction = next(iter(standin.transactions))
    for fail in ("timeout", "busy", ("refuse", 409, "subscription_locked_renewal")):
        standin.fail = fail
        ask(client, tokens, "a", "cancel")
        ask(client, tokens, "b", "checkout", {"plan": "monthly"})
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None)
    text = caplog.text
    assert "paddle call failed" in text                      # the failures were seen, by kind
    for secret in (KEY, custom["mothli_checkout"], transaction, sub_id("a"),
                   row(database, a)["paddle_customer_id"], PORTAL_TOKEN, "custom_data",
                   "mothli_reader", "effective_from", "stand-in"):
        assert secret not in text, secret
