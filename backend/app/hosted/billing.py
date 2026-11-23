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
   developer.paddle.com/build/transactions/custom-data). An unknown or erased account is recorded
   and ignored.
4. In one transaction on the billing role, with row-level security set to that account: the
   subscription's status and dates are written only if the message is newer than the last one
   applied (Paddle does not keep the order of delivery), and the message's id, type, time,
   account and outcome are recorded; a message whose id is already recorded changes nothing
   (Paddle may deliver one more than once: developer.paddle.com/webhooks/respond-to-webhooks).
5. 200, well inside Paddle's five seconds.

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
from starlette.concurrency import run_in_threadpool

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["hosted-billing"])

# A subscription message is a few kilobytes; nothing larger is read.
_MAX_BODY = 256 * 1024
_EVENT_ID = re.compile(r"evt_[a-z0-9]{26}")
_EVENT_TYPE = re.compile(r"subscription\.[a-z_]{1,32}")
_SUBSCRIPTION_ID = re.compile(r"sub_[a-z0-9]{26}")
_CUSTOMER_ID = re.compile(r"ctm_[a-z0-9]{26}")
_STATUSES = frozenset({"active", "trialing", "past_due", "paused", "canceled"})
# Paddle's statuses that keep access (developer.paddle.com/build/subscriptions/
# provision-access-webhooks): trialing as active, and past_due while Paddle retries a payment.
_LIVE = frozenset({"active", "trialing", "past_due"})
# The scheduled changes that end access on their date: a cancel to the end of what was paid, a
# pause (Mothli never offers one; a paused plan is ended).
_ENDING_CHANGES = frozenset({"cancel", "pause"})
_PLAN_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 49))"


class Malformed(ValueError):
    """A Mothli message, correctly signed, that cannot be read: refused, nothing written."""


@dataclass(frozen=True)
class SubscriptionMessage:
    event_id: str
    event_type: str
    occurred_at: datetime
    subscription_id: str
    customer_id: str
    status: str
    period_ends_at: datetime | None
    ends_at: datetime | None
    named_reader: uuid.UUID | None


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


def _named_reader(custom_data) -> uuid.UUID | None:
    if not isinstance(custom_data, dict):
        return None
    try:
        return uuid.UUID(str(custom_data.get("mothli_reader")))
    except ValueError:
        return None


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
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    for item in items:
        price = item.get("price") if isinstance(item, dict) else None
        if not isinstance(price, dict) or price.get("id") not in prices:
            return None
    # Mothli's: from here on, a message that cannot be read is refused.
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
    return SubscriptionMessage(
        event_id=_id(payload.get("event_id"), _EVENT_ID),
        event_type=event_type,
        occurred_at=_time(payload.get("occurred_at")),
        subscription_id=_id(data.get("id"), _SUBSCRIPTION_ID),
        customer_id=_id(data.get("customer_id"), _CUSTOMER_ID),
        status=status,
        period_ends_at=None if period is None else _time(period.get("ends_at"), optional=True),
        ends_at=ends_at,
        named_reader=_named_reader(data.get("custom_data")),
    )


class _Repeat(Exception):
    """The message's id is already recorded: everything this transaction did is undone."""


def _outcome(row, message: SubscriptionMessage) -> str:
    """What to do with a message for an account whose plan row is ``row`` (or None)."""
    if row is None or row[0] is None:
        return "applied"
    stored, status, applied_at = row
    if message.occurred_at <= applied_at:
        return "older"
    if stored == message.subscription_id:
        return "applied"
    # Another subscription for the same reader: a new one after a cancel takes the place of the
    # old; a late message about the old one never takes the place of a live one.
    if message.status in _LIVE or status not in _LIVE:
        return "applied"
    return "other_subscription"


def apply_message(dsn: str, message: SubscriptionMessage) -> str:
    """Apply one Mothli message on the billing role; the outcome recorded, or "repeat" when its
    id was already recorded (nothing is written then)."""
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn, conn.transaction():
            reader = conn.execute("SELECT public.paddle_reader(%s, %s::uuid)",
                                  (message.subscription_id, message.named_reader)).fetchone()[0]
            outcome = "no_account"
            if reader is not None:
                conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(reader),))
                conn.execute(_PLAN_LOCK, (reader,))
                row = conn.execute(
                    "SELECT paddle_subscription_id, paddle_status, paddle_occurred_at"
                    " FROM public.reader_plans WHERE owner_id=%s", (reader,)).fetchone()
                outcome = _outcome(row, message)
                if outcome == "applied":
                    conn.execute(
                        """INSERT INTO public.reader_plans AS plan
                             (owner_id, paddle_customer_id, paddle_subscription_id, paddle_status,
                              period_ends_at, ends_at, paddle_occurred_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (owner_id) DO UPDATE SET
                             paddle_customer_id = EXCLUDED.paddle_customer_id,
                             paddle_subscription_id = EXCLUDED.paddle_subscription_id,
                             paddle_status = EXCLUDED.paddle_status,
                             period_ends_at = EXCLUDED.period_ends_at,
                             ends_at = EXCLUDED.ends_at,
                             paddle_occurred_at = EXCLUDED.paddle_occurred_at,
                             updated_at = now()""",
                        (reader, message.customer_id, message.subscription_id, message.status,
                         message.period_ends_at, message.ends_at, message.occurred_at))
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
        message = read_message(json.loads(body), settings.paddle_prices())
    except (ValueError, RecursionError):
        _LOG.warning("paddle message refused: malformed")
        return Response(status_code=400)
    if message is None:
        return Response(status_code=200)
    # What was done is recorded with the message's id (paddle_events), not logged.
    await run_in_threadpool(apply_message, settings.hosted_billing_dsn.get_secret_value(), message)
    return Response(status_code=200)
