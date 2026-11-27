"""A refund or a chargeback ends the plan (migration 0044), on real PostgreSQL.

Paddle does not cancel a subscription when one of its transactions is refunded or charged back;
it sends an adjustment message. An approved refund of the whole transaction, or a chargeback, of
the subscription Mothli stored for a reader ends their plan at once and cancels the subscription
at Paddle with ``effective_from: immediately``; anything else changes nothing. Paddle's account is
shared with another product of the owner's (MySigner here): an adjustment about any subscription
Mothli did not store for a reader is answered and kept nowhere, and Paddle is never asked about it.

Through the hosted HTTP app, signed in where a route needs it, the billing role granted exactly
what the deployment's role script grants it. Paddle is a stand-in behind the real client: nothing
calls Paddle or any other outside service.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.hosted.paddle_api import PaddleApi
from app.hosted.paddle_check import check
from tests.hosted import test_paddle_messages_postgres as messages
from tests.hosted import test_paddle_subscribe_postgres as subscribing
from tests.hosted.test_companion_stream_postgres import (
    restore_caps,  # noqa: F401  (autouse: the hosted app sets this process's caps)
)
from tests.hosted.test_paddle_messages_postgres import (
    events,
    message,
    paddle_id,
    plan_of,
    post,
    stamp_of,
    sub_id,
)
from tests.hosted.test_paddle_subscribe_postgres import (
    KEY,
    MYSIGNER_CUSTOMER,
    MYSIGNER_SUB,
    PRICES,
    allowed,
    ask,
    plan,
    row,
    subscribe,
    written,
)

pytestmark = pytest.mark.postgres

admin_dsn = messages.admin_dsn
database = messages.database
tenant_dsn = messages.tenant_dsn
readers = messages.readers
make_app = messages.make_app
billing_dsn = messages.billing_dsn
paddle = messages.paddle
shop = subscribing.shop

CANCEL_NOW = {"effective_from": "immediately"}


def adjustment(sub: str | None, customer: str, *, action: str = "refund", kind: str = "full",
               status: str = "approved", event_type: str = "adjustment.created",
               event: str | None = None, at: datetime | None = None) -> dict:
    """An adjustment message as Paddle sends it (developer.paddle.com/webhooks/adjustments)."""
    at = at or datetime.now(UTC)
    return {"event_id": event or paddle_id("evt_", uuid.uuid4().hex),
            "event_type": event_type, "occurred_at": stamp_of(at),
            "notification_id": paddle_id("ntf_", uuid.uuid4().hex),
            "data": {"id": paddle_id("adj_", uuid.uuid4().hex), "action": action, "type": kind,
                     "status": status, "reason": "standin",
                     "transaction_id": paddle_id("txn_", uuid.uuid4().hex),
                     "subscription_id": sub, "customer_id": customer, "currency_code": "USD",
                     "items": [], "totals": {"total": "1299", "currency_code": "USD"},
                     "created_at": stamp_of(at), "updated_at": stamp_of(at)}}


def paying(shop, readers, label: str = "a", sub: str | None = None) -> tuple:
    """A reader paying through the whole way in; (reader, subscription, customer)."""
    client, tokens, standin = shop
    owner, sub = readers[label], sub or sub_id(label)
    subscribe(client, tokens, standin, label, owner, sub)
    standin.requests.clear()
    return owner, sub, standin.subscriptions[sub]["customer_id"]


def refunded_at(dsn: str, owner):
    return row(dsn, owner)["refunded_at"]


def calls(standin) -> list[tuple]:
    return [(method, path, body) for method, path, body, _h in standin.requests]


# ---- what ends the plan ------------------------------------------------------------------------

def test_an_approved_full_refund_ends_the_plan_at_once_and_cancels_it_at_paddle(
        shop, database, readers):
    """The break it catches: a refunded reader still talking with Mothli, or charged again at
    renewal because their subscription was left running at Paddle; the cancel asked for the end
    of the period instead of at once; Paddle's own cancelled message afterwards, or the same
    refund again, bringing anything back or calling Paddle twice."""
    client, tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    assert post(client, adjustment(sub, customer), seed=False).status_code == 200

    assert plan_of(database, a) == ("ended", "plan", None)
    assert calls(standin) == [("POST", f"/subscriptions/{sub}/cancel", CANCEL_NOW)]
    kept = row(database, a)
    assert kept["paddle_status"] == "canceled" and kept["refunded_at"] is not None
    assert datetime.fromisoformat(kept["ends_at"]) <= datetime.now(UTC)
    assert [(kind, owner, outcome) for _e, kind, owner, outcome in events(database)][-1] == (
        "adjustment.created", a, "applied")
    shown = plan(client, tokens, "a")
    assert (shown["state"], shown["why"], shown["subscribed"]) == ("ended", "plan", False)

    # Paddle's own message about the cancel, and the same refund again: nothing more.
    standin.requests.clear()
    gone = message(sub, "canceled", customer=customer, event_type="subscription.canceled",
                   at=datetime.now(UTC) + timedelta(minutes=5))
    assert post(client, gone, seed=False).status_code == 200
    before = (row(database, a), events(database))
    again = adjustment(sub, customer)
    for payload in (again, again):
        assert post(client, payload, seed=False).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    assert row(database, a)["refunded_at"] == before[0]["refunded_at"]
    assert [outcome for *_x, outcome in events(database)][len(before[1]):] == ["unchanged"]
    assert standin.requests == []


def test_a_chargeback_ends_the_plan_whatever_its_amount(shop, database, readers):
    """The break it catches: a reader whose payment was disputed and taken back still paying in
    Mothli's eyes and renewing at Paddle."""
    client, _tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    payload = adjustment(sub, customer, action="chargeback", kind="partial")
    assert post(client, payload, seed=False).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    assert calls(standin) == [("POST", f"/subscriptions/{sub}/cancel", CANCEL_NOW)]


def test_a_refund_waiting_for_approval_changes_nothing_until_approved(shop, database, readers):
    """The break it catches: a plan ended by a refund Paddle has not approved yet (and may
    reject), or one Paddle rejected; or the approval, when it comes, missed."""
    client, _tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    for status, kind in (("pending_approval", "adjustment.created"),
                         ("rejected", "adjustment.updated"),
                         ("pending_approval", "adjustment.created")):
        payload = adjustment(sub, customer, status=status, event_type=kind)
        assert post(client, payload, seed=False).status_code == 200
        assert plan_of(database, a) == ("paying", None, "paddle"), status
    assert standin.requests == [] and refunded_at(database, a) is None
    assert [outcome for *_x, outcome in events(database)][-3:] == ["unchanged"] * 3

    approved = adjustment(sub, customer, event_type="adjustment.updated")
    assert post(client, approved, seed=False).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    assert calls(standin) == [("POST", f"/subscriptions/{sub}/cancel", CANCEL_NOW)]


@pytest.mark.parametrize("action, kind", [
    ("refund", "partial"),                 # some money back: the plan goes on
    ("credit", "full"),
    ("chargeback_warning", "full"),
    ("chargeback_reverse", "full"),
    ("credit_reverse", "full"),
])
def test_a_partial_refund_a_credit_or_a_warning_changes_nothing(shop, database, readers, action,
                                                                kind):
    """The break it catches: a plan ended by an adjustment that takes nothing back for good (a
    partial refund the owner gives as a kindness, a credit, a warning of a dispute or the reversal
    of one)."""
    client, _tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    payload = adjustment(sub, customer, action=action, kind=kind)
    assert post(client, payload, seed=False).status_code == 200
    assert plan_of(database, a) == ("paying", None, "paddle")
    assert refunded_at(database, a) is None and standin.requests == []
    assert events(database)[-1][1:] == ("adjustment.created", a, "unchanged")


# ---- order, failures and the regular check ------------------------------------------------------

def test_messages_after_a_refund_never_bring_the_plan_back_and_the_check_cancels_it(
        shop, billing_dsn, database, readers):
    """The break it catches: Paddle unreachable when the refund came, and then a message about
    the still-active subscription (newer or older, since Paddle does not keep the order), or the
    regular check's answer from Paddle, making the refunded reader paying again; or the check
    never asking for the cancel again, so the subscription renews."""
    client, _tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    standin.fail = "down"
    assert post(client, adjustment(sub, customer), seed=False).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    assert row(database, a)["paddle_status"] == "active"           # Paddle not told yet
    standin.fail = None

    # (Newer than what was applied, yet older than the stand-in's clock, as Paddle's own answers
    # are newer than any message it sent before them.)
    now = datetime.now(UTC)
    for at in (now, now - timedelta(days=1)):
        later = message(sub, "active", customer=customer, at=at)
        assert post(client, later, seed=False).status_code == 200
        assert plan_of(database, a) == ("ended", "plan", None), at

    standin.requests.clear()
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(standin))
    assert check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None) == (1, 2, 0)
    assert calls(standin) == [("GET", f"/subscriptions/{sub}", None),
                              ("POST", f"/subscriptions/{sub}/cancel", CANCEL_NOW)]
    assert row(database, a)["paddle_status"] == "canceled"
    assert plan_of(database, a) == ("ended", "plan", None)
    allowed(standin, {sub: customer})

    # Cancelled at Paddle: the next check asks for nothing more.
    standin.requests.clear()
    check(billing_dsn, api, PRICES, size=50, pause=0, sleep=lambda _s: None)
    assert [method for method, _p, _b in calls(standin)] == ["GET"]


def test_a_new_plan_after_a_refund_is_live_and_the_old_one_is_cancelled_first(
        shop, database, readers):
    """The break it catches: a refunded reader who subscribes again kept out by the old
    subscription's refund; or a new checkout opened while the refunded subscription is still
    running at Paddle (once the new one is stored, Mothli forgets the old one, which renews)."""
    client, tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    standin.fail = "down"
    assert post(client, adjustment(sub, customer), seed=False).status_code == 200
    assert plan_of(database, a)[0] == "ended"
    # Paddle still unreachable: no checkout yet.
    made = len(standin.transactions)
    response = ask(client, tokens, "a", "checkout", {"plan": "monthly"})
    assert (response.status_code, response.json()["detail"]["code"]) == (503,
                                                                        "paddle_unavailable")
    assert len(standin.transactions) == made

    standin.fail = None
    standin.requests.clear()
    response = ask(client, tokens, "a", "checkout", {"plan": "monthly"})
    assert response.status_code == 200, response.text
    assert [(method, path) for method, path, _b in calls(standin)] == [
        ("POST", f"/subscriptions/{sub}/cancel"), ("POST", "/transactions")]
    assert standin.subscriptions[sub]["status"] == "canceled"
    custom = standin.transactions[response.json()["transaction_id"]]["custom_data"]
    again = message(sub_id("again"), customer=customer, custom=custom,
                    event_type="subscription.created", at=standin.clock + timedelta(minutes=1))
    assert post(client, again, seed=False).status_code == 200
    assert plan_of(database, a) == ("paying", None, "paddle")
    kept = row(database, a)
    assert (kept["paddle_subscription_id"], kept["refunded_at"], kept["ends_at"]) == (
        sub_id("again"), None, None)


def test_a_refunded_plan_already_cancelled_at_paddle_is_taken_as_cancelled(
        shop, database, readers):
    """The break it catches: a refunded subscription the owner already cancelled in Paddle's
    dashboard asked for again and again, and the reader never able to subscribe again."""
    client, tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    standin.subscriptions[sub].update(status="canceled", current_billing_period=None)
    assert post(client, adjustment(sub, customer), seed=False).status_code == 200
    assert calls(standin) == [("POST", f"/subscriptions/{sub}/cancel", CANCEL_NOW),
                              ("GET", f"/subscriptions/{sub}", None)]
    assert row(database, a)["paddle_status"] == "canceled"
    assert plan_of(database, a) == ("ended", "plan", None)


def test_without_the_api_key_the_plan_still_ends_and_the_operator_is_told(paddle, database,
                                                                          readers, caplog):
    """The break it catches: a refund that cannot end the plan because this server cannot call
    Paddle, or a server that says nothing about the subscription it could not cancel."""
    a = readers["a"]
    sub, customer = sub_id("a"), paddle_id("ctm_", "reader")
    assert post(paddle, message(sub, reader=a)).status_code == 200
    caplog.set_level(logging.WARNING)
    assert post(paddle, adjustment(sub, customer), seed=False).status_code == 200
    assert plan_of(database, a) == ("ended", "plan", None)
    assert "no PADDLE_API_KEY" in caplog.text and sub not in caplog.text


# ---- the shared Paddle account -----------------------------------------------------------------

def test_adjustments_not_about_a_stored_subscription_are_answered_and_kept_nowhere(
        shop, database, readers, caplog):
    """The break it catches: the owner's other product's refund or chargeback (or one about a
    subscription Mothli never stored, or about another customer, or with no subscription at all)
    recorded, changing a plan, or making Mothli call Paddle about it; an unreadable one refused
    so that Paddle keeps retrying the other product's messages; an unsigned one let in."""
    client, _tokens, standin = shop
    a, sub, customer = paying(shop, readers)
    caplog.set_level(logging.DEBUG)
    before = written(database)
    bad_id = adjustment(sub, customer)
    bad_id["event_id"] = "evt_NOT-AN-ID"
    no_time = adjustment(sub, customer)
    del no_time["occurred_at"]
    no_data = adjustment(sub, customer)
    no_data["data"] = ["not", "a", "dict"]
    for payload in (adjustment(MYSIGNER_SUB, MYSIGNER_CUSTOMER),
                    adjustment(MYSIGNER_SUB, MYSIGNER_CUSTOMER, action="chargeback"),
                    adjustment(sub_id("neverstored"), customer),
                    adjustment(sub, MYSIGNER_CUSTOMER),
                    adjustment(MYSIGNER_SUB, customer),
                    adjustment(None, customer),
                    adjustment(sub, customer, event_type="adjustment.deleted"),
                    bad_id, no_time, no_data):
        assert post(client, payload, seed=False).status_code == 200
    assert written(database) == before
    assert plan_of(database, a) == ("paying", None, "paddle")
    assert standin.requests == []
    assert MYSIGNER_SUB not in caplog.text and MYSIGNER_CUSTOMER not in caplog.text

    unsigned = adjustment(sub, customer)
    assert post(client, unsigned, None, seed=False).status_code == 400
    assert written(database) == before and plan_of(database, a)[0] == "paying"
