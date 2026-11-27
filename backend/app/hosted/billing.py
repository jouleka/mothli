"""Paddle's messages: how a reader becomes a paying one (ADR 0045 sections 1 and 7, step 3).

Paddle calls ``POST /api/billing/paddle`` with a signed message ("webhook") whenever a
subscription changes. There is no sign-in and no CSRF check on this one route, because Paddle is
the caller; the signature is the only thing that lets a message in. The route exists only when
``PADDLE_WEBHOOK_SECRET`` is set (``app.main``). For each message, in order:

1. The raw body is read, at most ``_MAX_BODY`` bytes, before anything parses it, and the
   ``Paddle-Signature`` header (``ts=...;h1=...``) is checked: HMAC-SHA256 of ``ts``, a colon and
   the raw body, keyed with the destination's secret as it is, compared in constant time with
   every ``h1`` the header carries (Paddle may send more than one while a secret is rotated), and
   ``ts`` within ``PADDLE_WEBHOOK_TOLERANCE_SECONDS`` of the server's clock
   (developer.paddle.com/webhooks/signature-verification). A message that fails is refused and
   nothing is read from it or written.
2. Paddle's account is shared with another product of the owner's, whose messages come here too.
   A message is Mothli's only when it is about a subscription and every item on it is one of
   Mothli's own price ids (``PADDLE_PRICE_MONTHLY`` and ``PADDLE_PRICE_YEARLY``, old ones kept).
   Any other message is answered 200 and kept nowhere: not its ids, not its customer, nothing.
   Custom data on Paddle's product and prices is not read; the price ids decide. A reader named in
   the custom data of a message about another price is never looked at.
3. The account: the one that holds the message's subscription, else, for a subscription Mothli has
   not seen, the reader named by ``mothli_reader`` in the subscription's custom data (Paddle copies
   it from the transaction that made the subscription:
   developer.paddle.com/build/transactions/custom-data), and only when the same custom data
   carries ``mothli_checkout``, a checkout token the server made for that same reader when it
   created the transaction (``app/hosted/subscribe.py``), unused, however old; the token is used
   up when the subscription is first applied (ADR 0045 step 4). Custom data alone proves nothing:
   anyone with the public client token can open a checkout naming any reader. A message naming an
   account with no such checkout, an unknown account or an erased one is recorded with no account
   and ignored.
4. In one transaction on the billing role, with row-level security set to that account: the
   subscription's status and dates are written only if the message is newer than the last one
   applied (Paddle does not keep the order of delivery), and the message's id, type, time,
   account and outcome are recorded; a message whose id is already recorded changes nothing
   (Paddle may deliver one more than once: developer.paddle.com/webhooks/respond-to-webhooks).
5. 200, well inside Paddle's five seconds.

A refund or a chargeback ends the plan (migration 0044). Paddle does not cancel a subscription
when one of its transactions is refunded; it sends an adjustment message (``adjustment.created``,
and ``adjustment.updated`` when Paddle approves or rejects a refund that waited for its review:
developer.paddle.com/webhooks/adjustments). An adjustment carries no price, so it is Mothli's only
when its ``subscription_id`` and ``customer_id`` are exactly the subscription and customer Mothli
stored for a reader; any other is answered 200 and kept nowhere, as above. Of Mothli's, an
approved refund of the whole transaction (``action`` refund, ``type`` full, ``status`` approved)
or a chargeback (not rejected or reversed) ends the reader's plan at once (``refunded_at``, and
``ends_at`` set to that moment), and after the answer is sent the subscription is cancelled at
Paddle with ``effective_from: immediately`` so it never renews (``subscribe.cancel_refunded``; the
regular check asks again while Paddle has not cancelled it). A partial refund, a refund still
waiting for approval or rejected, and a credit change nothing; each is recorded once by its id.

The logs say only that a message was refused, and why (its signature, or a Mothli message that
cannot be read): never the secret, the signature, an email address, an id or the payload. What
was done with each Mothli message is recorded in ``paddle_events``, not logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from fastapi import APIRouter, Request, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["hosted-billing"])

# A subscription message is a few kilobytes; nothing larger is read.
_MAX_BODY = 256 * 1024
_EVENT_ID = re.compile(r"evt_[a-z0-9]{26}")
_EVENT_TYPE = re.compile(r"subscription\.[a-z_]{1,32}")
_ADJUSTMENT_TYPE = re.compile(r"adjustment\.(created|updated)")
_SUBSCRIPTION_ID = re.compile(r"sub_[a-z0-9]{26}")
_CUSTOMER_ID = re.compile(r"ctm_[a-z0-9]{26}")
_STATUSES = frozenset({"active", "trialing", "past_due", "paused", "canceled"})
# Paddle's statuses that keep access (developer.paddle.com/build/subscriptions/
# provision-access-webhooks): trialing as active, and past_due while Paddle retries a payment.
_LIVE = frozenset({"active", "trialing", "past_due"})
# The statuses of a plan that has been paid for: the first one applied starts the reader's first
# paid month (ADR 0045 section 4), once per account, never moved (``first_paid_at``).
_PAID = frozenset({"active", "past_due"})
# The scheduled changes that end access on their date: a cancel to the end of what was paid, a
# pause (Mothli never offers one; a paused plan is ended).
_ENDING_CHANGES = frozenset({"cancel", "pause"})
_PLAN_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 49))"
# The checkout token the server puts in a transaction's custom data (``subscribe.py``): 32 random
# bytes, URL-safe base64 without padding.
_CHECKOUT_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class Malformed(ValueError):
    """A Mothli message, correctly signed, that cannot be read: refused, nothing written."""


@dataclass(frozen=True)
class SubscriptionMessage:
    """A subscription about Mothli's prices, as a message brought it, or as Paddle's API answered
    it (then ``event_id`` and ``event_type`` are None and ``occurred_at`` is the subscription's
    ``updated_at``)."""
    event_id: str | None
    event_type: str | None
    occurred_at: datetime
    subscription_id: str
    customer_id: str
    status: str
    period_ends_at: datetime | None
    ends_at: datetime | None
    named_reader: uuid.UUID | None
    price_id: str
    checkout: str | None = None


@dataclass(frozen=True)
class AdjustmentMessage:
    """An adjustment (a refund, a credit or a chargeback) about some subscription, as a message
    brought it; Mothli's only if the subscription and customer are ones Mothli stored."""
    event_id: str
    event_type: str
    occurred_at: datetime
    subscription_id: str
    customer_id: str
    ends_plan: bool


def checkout_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def verify_signature(header: str | None, body: bytes, secret: str, now: float,
                     tolerance: int) -> bool:
    """Whether ``body`` was signed by Paddle with ``secret`` within ``tolerance`` seconds of
    ``now``. Every ``h1`` in the header is compared, in constant time."""
    if not header or not secret:
        return False
    stamp, signatures = None, []
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            stamp = value
        elif key == "h1":
            signatures.append(value)
    if stamp is None or not stamp.isdigit() or len(stamp) > 12 or not signatures:
        return False
    if abs(now - int(stamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    matched = False
    for signature in signatures:
        matched |= hmac.compare_digest(expected, signature.encode("ascii", "replace").decode())
    return matched


def _time(value, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise Malformed("a time is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Malformed("a time is not RFC 3339") from exc
    if parsed.tzinfo is None:
        raise Malformed("a time has no zone")
    return parsed


def _id(value, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Malformed("an id is malformed")
    return value


def _named(custom_data) -> tuple[uuid.UUID | None, str | None]:
    """The reader and the checkout token the custom data names, each None when absent."""
    if not isinstance(custom_data, dict):
        return None, None
    try:
        reader = uuid.UUID(str(custom_data.get("mothli_reader")))
    except ValueError:
        reader = None
    token = custom_data.get("mothli_checkout")
    if not isinstance(token, str) or not _CHECKOUT_TOKEN.fullmatch(token):
        token = None
    return reader, token


def read_message(payload, prices: frozenset[str]) -> SubscriptionMessage | None:
    """The subscription message ``payload`` as Mothli applies it, or None when it is not
    Mothli's: not about a subscription, or not every item is one of ``prices``. Raises
    ``Malformed`` for a Mothli message that cannot be read."""
    if not isinstance(payload, dict) or not prices:
        return None
    event_type = payload.get("event_type")
    data = payload.get("data")
    if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type) \
            or not isinstance(data, dict):
        return None
    fields = _subscription(data, prices)
    if fields is None:
        return None
    return SubscriptionMessage(event_id=_id(payload.get("event_id"), _EVENT_ID),
                               event_type=event_type,
                               occurred_at=_time(payload.get("occurred_at")), **fields)


def _ends_plan(action, kind, status) -> bool:
    """Whether an adjustment ends the plan: a refund of the whole transaction once Paddle approved
    it (most live refunds wait for Paddle's review as ``pending_approval``, and may be rejected),
    or a chargeback, which Paddle makes itself when a payment is disputed (never waiting for a
    review), unless it is rejected or reversed. A partial refund, a credit, a chargeback warning
    or a reversal ends nothing."""
    if action == "refund":
        return kind == "full" and status == "approved"
    if action == "chargeback":
        return status not in ("rejected", "reversed")
    return False


def read_adjustment(payload) -> AdjustmentMessage | None:
    """The adjustment message ``payload``, or None when it is not one, names no subscription or
    cannot be read. Nothing here can tell Mothli's from another product's (an adjustment carries
    no price), so nothing raises: an unreadable one is ignored like any message not Mothli's."""
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("event_type")
    data = payload.get("data")
    if not isinstance(event_type, str) or not _ADJUSTMENT_TYPE.fullmatch(event_type) \
            or not isinstance(data, dict):
        return None
    try:
        return AdjustmentMessage(
            event_id=_id(payload.get("event_id"), _EVENT_ID), event_type=event_type,
            occurred_at=_time(payload.get("occurred_at")),
            subscription_id=_id(data.get("subscription_id"), _SUBSCRIPTION_ID),
            customer_id=_id(data.get("customer_id"), _CUSTOMER_ID),
            ends_plan=_ends_plan(data.get("action"), data.get("type"), data.get("status")))
    except Malformed:
        return None


def read_subscription(entity, prices: frozenset[str]) -> SubscriptionMessage | None:
    """A subscription as Paddle's API answers it (``GET /subscriptions/{id}``, or the answer to a
    cancel), read exactly as a message's ``data`` is, with its ``updated_at`` as the time; None
    when it is not Mothli's. Raises ``Malformed`` for a Mothli one that cannot be read."""
    if not isinstance(entity, dict) or not prices:
        return None
    fields = _subscription(entity, prices)
    if fields is None:
        return None
    return SubscriptionMessage(event_id=None, event_type=None,
                               occurred_at=_time(entity.get("updated_at")), **fields)


def _subscription(data: dict, prices: frozenset[str]) -> dict | None:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    for item in items:
        price = item.get("price") if isinstance(item, dict) else None
        if not isinstance(price, dict) or price.get("id") not in prices:
            return None
    # Mothli's: from here on, a subscription that cannot be read is refused.
    status = data.get("status")
    if status not in _STATUSES:
        raise Malformed("unknown subscription status")
    period = data.get("current_billing_period")
    change = data.get("scheduled_change")
    if period is not None and not isinstance(period, dict):
        raise Malformed("the billing period is malformed")
    if change is not None and not isinstance(change, dict):
        raise Malformed("the scheduled change is malformed")
    ends_at = None
    if change is not None and change.get("action") in _ENDING_CHANGES:
        ends_at = _time(change.get("effective_at"))
    reader, token = _named(data.get("custom_data"))
    return dict(
        subscription_id=_id(data.get("id"), _SUBSCRIPTION_ID),
        customer_id=_id(data.get("customer_id"), _CUSTOMER_ID),
        status=status,
        period_ends_at=None if period is None else _time(period.get("ends_at"), optional=True),
        ends_at=ends_at,
        named_reader=reader,
        price_id=items[0]["price"]["id"],
        checkout=token,
    )


class _Repeat(Exception):
    """The message's id is already recorded: everything this transaction did is undone."""


def _outcome(row, message: SubscriptionMessage) -> str:
    """What to do with a message for an account whose plan row is ``row`` (or None)."""
    if row is None or row[0] is None:
        return "applied"
    stored, _status, applied_at, stored_live = row
    if message.occurred_at <= applied_at:
        return "older"
    if stored == message.subscription_id:
        return "applied"
    # Another subscription naming the same reader takes the place of theirs only once theirs is
    # no longer live (a new plan after a cancel, or after its scheduled end). While their own plan
    # is live, any other subscription, live or not, is recorded and changes nothing: one made by
    # someone else naming them cannot displace it, and cancelling that one cannot end theirs.
    if not stored_live:
        return "applied"
    return "other_subscription"


def _checkout_open(conn, reader: uuid.UUID, token: str | None) -> bool:
    """Whether ``token`` is a checkout the server made for ``reader`` and unused, however old (the
    transaction is set to ``reader``, so row-level security shows only theirs). ``expires_at`` is
    kept as a record and decides nothing: a payment made from Paddle's late reminder email must
    still make its reader paying, and the token is unguessable, one reader's and used once."""
    if token is None:
        return False
    return conn.execute(
        "SELECT 1 FROM public.paddle_checkouts WHERE owner_id=%s AND token_digest=%s"
        " AND used_at IS NULL FOR UPDATE",
        (reader, checkout_digest(token))).fetchone() is not None


def _apply(conn, message: SubscriptionMessage) -> tuple[str, uuid.UUID | None]:
    """Apply one subscription in the open transaction: the outcome and the account, or None."""
    reader = conn.execute("SELECT public.paddle_reader(%s, %s::uuid)",
                          (message.subscription_id, message.named_reader)).fetchone()[0]
    if reader is None:
        return "no_account", None
    conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(reader),))
    conn.execute(_PLAN_LOCK, (reader,))
    row = conn.execute(
        "SELECT paddle_subscription_id, paddle_status, paddle_occurred_at,"
        " paddle_status IN ('active', 'trialing', 'past_due')"
        " AND (ends_at IS NULL OR now() < ends_at)"
        " FROM public.reader_plans WHERE owner_id=%s", (reader,)).fetchone()
    stored = row is not None and row[0] == message.subscription_id
    # A subscription Mothli has not stored is the named reader's only with their own checkout.
    if not stored and not _checkout_open(conn, reader, message.checkout):
        return "no_account", None
    outcome = _outcome(row, message)
    if outcome == "applied":
        conn.execute(
            """INSERT INTO public.reader_plans AS plan
                 (owner_id, paddle_customer_id, paddle_subscription_id, paddle_status,
                  period_ends_at, ends_at, paddle_occurred_at, first_paid_at, paddle_price_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s)
               ON CONFLICT (owner_id) DO UPDATE SET
                 paddle_customer_id = EXCLUDED.paddle_customer_id,
                 paddle_subscription_id = EXCLUDED.paddle_subscription_id,
                 paddle_status = EXCLUDED.paddle_status,
                 period_ends_at = EXCLUDED.period_ends_at,
                 -- A subscription refunded or charged back stays ended from that moment, whatever
                 -- Paddle says of it later (migration 0044); a new subscription starts afresh.
                 ends_at = CASE WHEN plan.paddle_subscription_id = EXCLUDED.paddle_subscription_id
                                THEN LEAST(EXCLUDED.ends_at, plan.refunded_at)
                                ELSE EXCLUDED.ends_at END,
                 refunded_at = CASE
                   WHEN plan.paddle_subscription_id = EXCLUDED.paddle_subscription_id
                   THEN plan.refunded_at END,
                 paddle_occurred_at = EXCLUDED.paddle_occurred_at,
                 first_paid_at = COALESCE(plan.first_paid_at, EXCLUDED.first_paid_at),
                 paddle_price_id = EXCLUDED.paddle_price_id,
                 updated_at = now()""",
            (reader, message.customer_id, message.subscription_id, message.status,
             message.period_ends_at, message.ends_at, message.occurred_at,
             message.status in _PAID, message.price_id))
        if not stored:
            conn.execute("UPDATE public.paddle_checkouts SET used_at = now()"
                         " WHERE owner_id=%s AND token_digest=%s",
                         (reader, checkout_digest(message.checkout)))
    return outcome, reader


def apply_message(dsn: str, message: SubscriptionMessage) -> str:
    """Apply one Mothli subscription on the billing role; the outcome. A message is recorded by
    its id ("repeat" when its id was already recorded: nothing is written then); a subscription
    read from Paddle's API has no id and is applied the same way, unrecorded."""
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn, conn.transaction():
            outcome, reader = _apply(conn, message)
            if message.event_id is not None:
                recorded = conn.execute(
                    """INSERT INTO public.paddle_events
                         (event_id, event_type, occurred_at, owner_id, outcome)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING""",
                    (message.event_id, message.event_type, message.occurred_at, reader, outcome))
                if recorded.rowcount != 1:
                    raise _Repeat
    except _Repeat:
        return "repeat"
    return outcome


@dataclass(frozen=True)
class AdjustmentOutcome:
    """What an adjustment did: ``outcome`` (``not_ours``: nothing was recorded; ``repeat``,
    ``applied`` or ``unchanged``), the reader it was for, and whether the subscription is still
    to be cancelled at Paddle (the plan ended by a refund, and Paddle not yet saying cancelled)."""
    outcome: str
    reader: uuid.UUID | None = None
    cancel_due: bool = False


def _end_by_refund(conn, message: AdjustmentMessage) -> AdjustmentOutcome:
    """Apply one adjustment in the open transaction. It is Mothli's only when its subscription
    is the one stored for a reader, with the same customer; nothing is written otherwise."""
    reader = conn.execute("SELECT public.paddle_reader(%s, NULL::uuid)",
                          (message.subscription_id,)).fetchone()[0]
    if reader is None:
        return AdjustmentOutcome("not_ours")
    conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(reader),))
    conn.execute(_PLAN_LOCK, (reader,))
    row = conn.execute(
        "SELECT paddle_subscription_id, paddle_customer_id, paddle_status, refunded_at"
        " FROM public.reader_plans WHERE owner_id=%s", (reader,)).fetchone()
    if row is None or row[0] != message.subscription_id or row[1] != message.customer_id:
        return AdjustmentOutcome("not_ours")
    _sub, _customer, status, refunded_at = row
    outcome = "unchanged"
    if message.ends_plan and refunded_at is None:
        conn.execute("UPDATE public.reader_plans SET refunded_at = now(),"
                     " ends_at = LEAST(ends_at, now()), updated_at = now() WHERE owner_id=%s",
                     (reader,))
        outcome = "applied"
    ended = message.ends_plan or refunded_at is not None
    return AdjustmentOutcome(outcome, reader, ended and status != "canceled")


def apply_adjustment(dsn: str, message: AdjustmentMessage) -> AdjustmentOutcome:
    """Apply one adjustment on the billing role, recorded once by its id when it is Mothli's."""
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn, conn.transaction():
            done = _end_by_refund(conn, message)
            if done.outcome == "not_ours":
                return done
            recorded = conn.execute(
                """INSERT INTO public.paddle_events
                     (event_id, event_type, occurred_at, owner_id, outcome)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING""",
                (message.event_id, message.event_type, message.occurred_at, done.reader,
                 done.outcome))
            if recorded.rowcount != 1:
                raise _Repeat
    except _Repeat:
        return AdjustmentOutcome("repeat")
    return done


def _cancel_refunded(request: Request, reader: uuid.UUID) -> BackgroundTask | None:
    """The cancel at Paddle of a plan a refund ended, run after the answer is sent (Paddle waits
    five seconds at most). Without an API key it cannot be made here: the operator is told."""
    settings = request.app.state.settings
    if settings.paddle_api_key is None:
        _LOG.warning("a refunded plan could not be cancelled at Paddle: no PADDLE_API_KEY; "
                     "cancel it in Paddle (runbook)")
        return None
    from app.hosted.subscribe import cancel_refunded, paddle_api

    return BackgroundTask(cancel_refunded,
                          settings.hosted_billing_dsn.get_secret_value(), paddle_api(request),
                          settings.paddle_prices(), reader)


async def _raw_body(request: Request) -> bytes | None:
    """The body exactly as sent, or None past ``_MAX_BODY``."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY:
            return None
    return bytes(body)


@router.post("/paddle", include_in_schema=False)
async def paddle_message(request: Request) -> Response:
    settings = request.app.state.settings
    body = await _raw_body(request)
    if body is None:
        return Response(status_code=413)
    if not verify_signature(request.headers.get("paddle-signature"), body,
                            settings.paddle_webhook_secret.get_secret_value(), time.time(),
                            settings.paddle_webhook_tolerance_seconds):
        _LOG.warning("paddle message refused: signature")
        return Response(status_code=400)
    try:
        payload = json.loads(body)
        message = read_message(payload, settings.paddle_prices())
    except (ValueError, RecursionError):
        _LOG.warning("paddle message refused: malformed")
        return Response(status_code=400)
    if message is None:
        adjustment = read_adjustment(payload)
        if adjustment is None:
            return Response(status_code=200)
        done = await run_in_threadpool(
            apply_adjustment, settings.hosted_billing_dsn.get_secret_value(), adjustment)
        task = _cancel_refunded(request, done.reader) if done.cancel_due else None
        return Response(status_code=200, background=task)
    # What was done is recorded with the message's id (paddle_events), not logged.
    await run_in_threadpool(apply_message, settings.hosted_billing_dsn.get_secret_value(), message)
    return Response(status_code=200)
