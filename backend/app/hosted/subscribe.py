"""A signed-in reader's plan through Paddle (ADR 0045 sections 6 and 7, step 4, the server half).

``GET /api/billing/plan`` says where the reader stands, for the account sheet, with ``manage``
(whether the four routes below exist here) and ``checkout`` (``checkout_setup``: what the plan
sheet's Paddle.js needs, public by design, or null where no checkout can open). Four routes act on
the reader's own plan, each signed in with the CSRF token, each only for the session's reader
(no route takes an account, a subscription or a customer id from the browser), and each writing
only through the billing role:

- ``POST /api/billing/checkout`` ``{"plan": "monthly" | "yearly"}``: keeps the digest of a fresh
  checkout token for the reader (``paddle_checkouts``, seven days), then creates the Paddle
  transaction for the plan's current price (the first id in its setting) with custom data naming
  the reader and that token, and answers ``{"transaction_id": "txn_..."}`` for Paddle.js
  to open checkout with. Refused while the reader has a live Paddle plan. A gifted reader may
  subscribe: the gift still wins while it lasts, and the plan pays from the day it starts.
- ``POST /api/billing/cancel``: schedules the cancel at the end of the period paid for.
- ``POST /api/billing/keep``: removes that scheduled cancel.
- ``POST /api/billing/portal``: a customer portal session for the reader's own customer and
  subscription; answers ``{"url": ...}``. The link is never stored and never logged.

And one public read, ``GET /api/billing/payment-link``: Paddle's environment and the public
client token, for Mothli's payment-link page, which a visitor may open signed out.

Cancel and keep apply Paddle's answer at once, through the same code as a message
(``billing.apply_message``: newer only, Mothli's prices only), and answer the plan as ``GET``
does. Paddle unreachable, slow or failing answers 503 ``paddle_unavailable`` and nothing is
written; Paddle refusing a cancel within 30 minutes of the renewal (``subscription_locked_*``,
developer.paddle.com/errors/subscriptions/subscription_locked_renewal) answers 409
``try_again_soon``. Nothing a reader is answered holds an amount, a count or a countdown.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from app.config import _items
from app.hosted.auth.api import _NO_STORE
from app.hosted.auth.tokens import new_token
from app.hosted.billing import Malformed, apply_message, checkout_digest, read_subscription
from app.hosted.paddle_api import PaddleApi, PaddleRefused, PaddleUnavailable
from app.hosted.tenant.api import _csrf_owner, _owner
from app.hosted.tenant.models import OwnerId

router = APIRouter(prefix="/api/billing", tags=["hosted-billing"])
plan_router = APIRouter(prefix="/api/billing", tags=["hosted-billing"])

_LIVE = ("active", "trialing", "past_due")
_FOR_GOOD = date(9999, 12, 31)
# How long a checkout the server made can bind a new subscription to its reader: past the time a
# reader may take to pay in an open checkout and Paddle's retries of a first message (about two
# days live: developer.paddle.com/webhooks/respond-to-webhooks).
CHECKOUT_DAYS = 7
# Checkouts one reader may start in a day: each is a call to Paddle, whose limit is per address.
CHECKOUTS_PER_DAY = 10

_UNAVAILABLE = ("paddle_unavailable",
                "I can’t reach our payment service just now, so nothing has changed. "
                "Please try again in a minute.")
_TRY_AGAIN_SOON = ("try_again_soon",
                   "Your plan is renewing right now. Please try again in a few minutes.")


def _refused(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status,
                        headers=_NO_STORE)


def paddle_api(request: Request) -> PaddleApi:
    """The app's Paddle client, made once (tests put a stand-in in its place)."""
    api = getattr(request.app.state, "paddle_api", None)
    if api is None:
        settings = request.app.state.settings
        api = request.app.state.paddle_api = PaddleApi(
            settings.paddle_api_key.get_secret_value(), settings.paddle_environment)
    return api


def _billing_dsn(request: Request) -> str:
    return request.app.state.settings.hosted_billing_dsn.get_secret_value()


@dataclass(frozen=True)
class Stored:
    subscription_id: str | None
    customer_id: str | None
    live: bool
    ends_at: datetime | None


def stored_plan(dsn: str, owner: uuid.UUID) -> Stored:
    """The reader's own stored Paddle plan, read on the billing role set to them."""
    with psycopg.connect(dsn, connect_timeout=3) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        row = conn.execute(
            "SELECT paddle_subscription_id, paddle_customer_id,"
            " COALESCE(paddle_status IN ('active', 'trialing', 'past_due')"
            " AND (ends_at IS NULL OR now() < ends_at), false), ends_at"
            " FROM public.reader_plans WHERE owner_id=%s", (owner,)).fetchone()
    return Stored(None, None, False, None) if row is None else Stored(*row)


def plan_payload(row: dict, monthly: frozenset[str], yearly: frozenset[str]) -> dict:
    """What the account sheet is told, from the reader's ``reader_plan_state`` and stored price.

    ``state`` and ``why`` as ``reader_plan_state`` answers them; ``plan``: what makes the reader
    paying (``gift`` while a gift lasts, else ``monthly`` or ``yearly`` for a live Paddle plan,
    else None); ``next_date``: the gift's last moment (None for a gift for good), the scheduled
    end of a cancelled plan, the renewal of a live one, or the end of the free weeks; and three
    yes-or-no answers: ``subscribed`` (a live Paddle plan the reader can cancel, keep or manage,
    even under a gift), ``cancel_scheduled``, ``payment_retrying``. No amount, no count."""
    live = row["paddle_status"] in _LIVE and (
        row["plan_ends_at"] is None or datetime.now(UTC) < row["plan_ends_at"])
    plan, next_date = None, None
    if row["paid_by"] == "gift":
        plan = "gift"
        if row["gift_until"] != _FOR_GOOD:
            next_date = datetime.combine(row["gift_until"], time.max, UTC)
    elif row["paid_by"] == "paddle":
        price = row["paddle_price_id"]
        plan = "monthly" if price in monthly else "yearly" if price in yearly else None
        next_date = row["plan_ends_at"] or row["period_ends_at"]
    elif row["state"] == "free":
        next_date = row["free_ends_at"]
    return {
        "state": row["state"],
        "why": row["why"],
        "plan": plan,
        "next_date": None if next_date is None else next_date.astimezone(UTC).isoformat(),
        "subscribed": live,
        "cancel_scheduled": live and row["plan_ends_at"] is not None,
        "payment_retrying": live and row["paddle_status"] == "past_due",
    }


def read_plan(settings, owner: uuid.UUID) -> dict:
    """The reader's plan, read as the reader on the tenant role (row-level security)."""
    with psycopg.connect(settings.hosted_tenant_dsn.get_secret_value(),
                         connect_timeout=3) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        cursor = conn.execute(
            "SELECT state.state, state.why, state.free_ends_at, state.paid_by, state.gift_until,"
            " state.paddle_status, state.period_ends_at, state.plan_ends_at,"
            " (SELECT kept.paddle_price_id FROM public.reader_plans AS kept"
            "  WHERE kept.owner_id = %s) AS paddle_price_id"
            " FROM public.reader_plan_state(%s::uuid, %s::integer, %s::numeric) AS state",
            (owner, owner, settings.hosted_free_days, str(settings.hosted_free_usd)))
        names = [column.name for column in cursor.description]
        row = dict(zip(names, cursor.fetchone(), strict=True))
    return plan_payload(row, frozenset(_items(settings.paddle_price_monthly)),
                        frozenset(_items(settings.paddle_price_yearly)))


def checkout_setup(settings) -> dict | None:
    """What Paddle.js in the reader's browser needs for the plan sheet, or None where this server
    cannot open a checkout (no API key, so no checkout route, or no client token): Paddle's
    environment, the public client token, and the price sold for each plan (the first id in its
    setting; None for a plan not on offer). Never a secret."""
    monthly = _items(settings.paddle_price_monthly)
    yearly = _items(settings.paddle_price_yearly)
    if settings.paddle_api_key is None or not settings.paddle_client_token \
            or not (monthly or yearly):
        return None
    return {"environment": settings.paddle_environment,
            "client_token": settings.paddle_client_token,
            "prices": {"monthly": monthly[0] if monthly else None,
                       "yearly": yearly[0] if yearly else None}}


async def _plan_response(request: Request, owner: OwnerId) -> JSONResponse:
    settings = request.app.state.settings
    payload = await run_in_threadpool(read_plan, settings, owner.value)
    # ``manage``: whether this server has the cancel, keep and portal routes at all.
    return JSONResponse({**payload, "manage": settings.paddle_api_key is not None,
                         "checkout": checkout_setup(settings)}, headers=_NO_STORE)


@plan_router.get("/plan")
async def plan(request: Request, owner: OwnerId = Depends(_owner)) -> JSONResponse:
    return await _plan_response(request, owner)


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["monthly", "yearly"]


def _record_checkout(dsn: str, owner: uuid.UUID, token: str) -> None:
    with psycopg.connect(dsn, connect_timeout=3) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        conn.execute("INSERT INTO public.paddle_checkouts (owner_id, token_digest, expires_at)"
                     " VALUES (%s, %s, now() + make_interval(days => %s))",
                     (owner, checkout_digest(token), CHECKOUT_DAYS))


def _checkouts_today(dsn: str, owner: uuid.UUID) -> int:
    with psycopg.connect(dsn, connect_timeout=3) as conn, conn.transaction():
        conn.execute("SELECT set_config('app.owner_id', %s, true)", (str(owner),))
        return conn.execute("SELECT count(*) FROM public.paddle_checkouts WHERE owner_id=%s"
                            " AND created_at > now() - interval '1 day'", (owner,)).fetchone()[0]


@router.get("/payment-link")
async def payment_link(request: Request) -> JSONResponse:
    """What Paddle.js needs on Mothli's payment-link page (``PADDLE_CHECKOUT_URL`` with
    ``_ptxn``), where the visitor may not be signed in on that device: Paddle's environment and
    the public client token, those two values and nothing else. 404 where no checkout opens."""
    setup = checkout_setup(request.app.state.settings)
    if setup is None:
        return _refused(404, "no_checkout", "The checkout isn’t open here just now.")
    return JSONResponse({"environment": setup["environment"],
                         "client_token": setup["client_token"]}, headers=_NO_STORE)


@router.post("/checkout")
async def checkout(body: CheckoutIn, request: Request,
                   owner: OwnerId = Depends(_csrf_owner)) -> JSONResponse:
    settings = request.app.state.settings
    prices = _items(settings.paddle_price_monthly if body.plan == "monthly"
                    else settings.paddle_price_yearly)
    if not prices:
        return _refused(404, "plan_not_sold", "That plan isn’t on offer right now.")
    dsn = _billing_dsn(request)
    stored = await run_in_threadpool(stored_plan, dsn, owner.value)
    if stored.live:
        return _refused(409, "already_subscribed", "You’re already on the plan!")
    if await run_in_threadpool(_checkouts_today, dsn, owner.value) >= CHECKOUTS_PER_DAY:
        return _refused(429, "checkout_busy",
                        "Let’s take a little break from checkouts. Please try again later.")
    # The token is kept before Paddle hears of it: a checkout whose token Mothli never stored
    # could be paid for and bind to no one. If Paddle then fails, the row is never used and
    # expires (it still counts toward the day's checkouts).
    token = new_token()
    try:
        await run_in_threadpool(_record_checkout, dsn, owner.value, token)
    except psycopg.Error:
        return _refused(503, *_UNAVAILABLE)
    try:
        transaction = await run_in_threadpool(
            paddle_api(request).create_transaction, prices[0],
            {"mothli_reader": str(owner.value), "mothli_checkout": token},
            settings.paddle_checkout_url)
    except (PaddleUnavailable, PaddleRefused):
        return _refused(503, *_UNAVAILABLE)
    return JSONResponse({"transaction_id": transaction}, headers=_NO_STORE)


def _apply_answer(request: Request, entity: dict) -> None:
    """Apply Paddle's answer about the reader's own subscription, as a message would be."""
    settings = request.app.state.settings
    try:
        message = read_subscription(entity, settings.paddle_prices())
    except Malformed:
        return
    if message is not None:
        apply_message(_billing_dsn(request), message)


async def _change(request: Request, owner: OwnerId, *, cancel: bool) -> JSONResponse:
    stored = await run_in_threadpool(stored_plan, _billing_dsn(request), owner.value)
    if not stored.live:
        return _refused(409, "no_plan", "There’s no plan here to change.")
    # Already as asked: nothing to send.
    if (stored.ends_at is not None) == cancel:
        return await _plan_response(request, owner)
    api = paddle_api(request)
    call = api.cancel if cancel else api.remove_scheduled_change
    try:
        entity = await run_in_threadpool(call, stored.subscription_id)
    except PaddleUnavailable:
        return _refused(503, *_UNAVAILABLE)
    except PaddleRefused as refused:
        if refused.code.startswith("subscription_locked"):
            return _refused(409, *_TRY_AGAIN_SOON)
        return _refused(503, *_UNAVAILABLE)
    if entity.get("id") == stored.subscription_id:
        await run_in_threadpool(_apply_answer, request, entity)
    return await _plan_response(request, owner)


@router.post("/cancel")
async def cancel(request: Request, owner: OwnerId = Depends(_csrf_owner)) -> JSONResponse:
    return await _change(request, owner, cancel=True)


@router.post("/keep")
async def keep(request: Request, owner: OwnerId = Depends(_csrf_owner)) -> JSONResponse:
    return await _change(request, owner, cancel=False)


@router.post("/portal")
async def portal(request: Request, owner: OwnerId = Depends(_csrf_owner)) -> JSONResponse:
    stored = await run_in_threadpool(stored_plan, _billing_dsn(request), owner.value)
    if stored.customer_id is None or stored.subscription_id is None:
        return _refused(409, "no_plan", "There’s no plan here to manage.")
    try:
        urls = await run_in_threadpool(paddle_api(request).portal, stored.customer_id,
                                       stored.subscription_id)
    except (PaddleUnavailable, PaddleRefused):
        return _refused(503, *_UNAVAILABLE)
    link = None
    for item in urls.get("subscriptions") or ():
        if isinstance(item, dict) and item.get("id") == stored.subscription_id and stored.live:
            link = item.get("update_subscription_payment_method")
    if link is None:
        general = urls.get("general")
        link = general.get("overview") if isinstance(general, dict) else None
    if not isinstance(link, str) or not link.startswith("https://"):
        return _refused(503, *_UNAVAILABLE)
    return JSONResponse({"url": link}, headers=_NO_STORE)
