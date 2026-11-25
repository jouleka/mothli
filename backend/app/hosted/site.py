"""What the public pages need, signed out (ADR 0045 step 8): ``GET /api/site``.

The front page, the terms, the privacy notice, the refunds page and the contact page are read by
visitors and by Paddle's reviewers without signing in. They need three things from the server:

- ``operator``: who runs Mothli, from the settings (``OPERATOR_LEGAL_NAME``, ``OPERATOR_COUNTRY``,
  ``OPERATOR_CONTACT_EMAIL``), or null while any of the three is missing, and the pages then say
  Mothli is not open for sign-ups here instead of publishing terms with holes;
- ``plan``: the two prices exactly as Paddle's catalog has them, read on the server by their ids
  (``GET /prices/{id}``), or null. Null where this server has no Paddle API key or no price for a
  plan, where Paddle cannot be reached or refuses (a key without ``price.read``, for example), and
  where the two answers do not hold together (not active, not billed every month and every year,
  not the same currency, not a whole positive amount). The page then shows the plan without
  figures: never a number that could contradict the checkout;
- ``writer``: the AI company this server's chat uses, by name only (``writer`` below), for the
  privacy page, or null where the chat is not open here.

The prices read are the ones sold now, the first id of ``PADDLE_PRICE_MONTHLY`` and of
``PADDLE_PRICE_YEARLY``, as the checkout uses (``subscribe.checkout_setup``). The Paddle account is
shared with another product of the owner's, so no other id is ever asked for. A good answer is
kept for ten minutes, a failed one for one, so a visitor never makes the server call Paddle more
than twice in that time, and while one read is under way other visitors are answered from what
is kept (or without figures) rather than waiting for it. Paddle's script is never loaded for any
of this: it stays on the plan sheet and the payment-link page.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import _items
from app.hosted.auth.api import _NO_STORE
from app.hosted.paddle_api import PaddleApi, PaddleRefused, PaddleUnavailable
from app.hosted.subscribe import paddle_api
from app.llm.client import _OPENAI_DEFAULT_BASE as OPENAI_DEFAULT_BASE
from app.llm.client import detect_provider

router = APIRouter(prefix="/api", tags=["hosted-site"])

FRESH_SECONDS = 600.0
FAILED_SECONDS = 60.0
_AMOUNT = re.compile(r"[1-9][0-9]{0,14}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_HOST = re.compile(r"[a-z0-9.-]{1,100}")


def operator(settings) -> dict | None:
    """Who runs Mothli, as the terms name them, or None while any detail is missing."""
    if not settings.operator_published():
        return None
    return {"legal_name": settings.operator_legal_name, "country": settings.operator_country,
            "contact_email": settings.operator_contact_email}


def writer(settings) -> str | None:
    """The AI company this server's chat sends readers' passages and questions to, by name, as
    the privacy page says it; None where the chat is not open here. Read from the same settings,
    by the same rule, that choose the model client (``LLMClient`` over ``Settings.llm_env``,
    ``app.llm.client.detect_provider``): an Anthropic key wins; otherwise an OpenAI key goes to
    OpenAI, unless ``OPENAI_BASE_URL`` points elsewhere, when the service is named by its host.
    A name only: never a key, a model or a full address."""
    if settings.deployment_mode != "hosted" or not settings.companion_chat():
        return None
    provider = detect_provider(settings.llm_env())
    if provider == "anthropic":
        return "Anthropic"
    if provider != "openai-compatible":
        return None
    base = (settings.openai_base_url or "").strip()
    if not base or base.rstrip("/") == OPENAI_DEFAULT_BASE:
        return "OpenAI"
    host = urlsplit(base).hostname
    return host if host and _HOST.fullmatch(host) else None


def sold_prices(settings) -> tuple[str, str] | None:
    """The monthly and yearly price ids sold now, or None where no figure can be read."""
    monthly = _items(settings.paddle_price_monthly)
    yearly = _items(settings.paddle_price_yearly)
    if settings.paddle_api_key is None or not monthly or not yearly:
        return None
    return monthly[0], yearly[0]


def _price(entity: dict, price_id: str, interval: str) -> tuple[str, str] | None:
    """A price's amount (in the currency's smallest unit) and currency, if it is the price asked
    for, active, and billed once every ``interval``."""
    cycle = entity.get("billing_cycle")
    unit = entity.get("unit_price")
    if (entity.get("id") != price_id or entity.get("status") != "active"
            or not isinstance(cycle, dict) or cycle.get("interval") != interval
            or cycle.get("frequency") != 1 or not isinstance(unit, dict)):
        return None
    amount, currency = unit.get("amount"), unit.get("currency_code")
    if not (isinstance(amount, str) and _AMOUNT.fullmatch(amount)
            and isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
        return None
    return amount, currency


def read_plan(api: PaddleApi, monthly_id: str, yearly_id: str) -> dict | None:
    """The two prices from Paddle, or None if either cannot be read or they do not agree."""
    try:
        monthly = _price(api.price(monthly_id), monthly_id, "month")
        yearly = _price(api.price(yearly_id), yearly_id, "year")
    except (PaddleUnavailable, PaddleRefused, ValueError):
        return None
    if monthly is None or yearly is None or monthly[1] != yearly[1]:
        return None
    return {"currency": monthly[1], "monthly": monthly[0], "yearly": yearly[0]}


class PlanFigures:
    """The prices read from Paddle, kept for a while (see the module's note)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._until = float("-inf")

    def get(self, ids: tuple[str, str], api: Callable[[], PaddleApi]) -> dict | None:
        if self._clock() < self._until:
            return self._value
        if not self._lock.acquire(blocking=False):
            # Another visitor's read is under way: answer from what is kept, if still good.
            return self._value if self._clock() < self._until else None
        try:
            if self._clock() >= self._until:
                value = read_plan(api(), *ids)
                self._value = value
                self._until = self._clock() + (FRESH_SECONDS if value else FAILED_SECONDS)
            return self._value
        finally:
            self._lock.release()


async def site_answer(request: Request) -> dict:
    """The operator's details, the plan's two prices and the chat's AI company, each or None:
    what ``GET /api/site`` answers and what the public pages' HTML is filled with
    (``public_pages``), from the same prices kept."""
    settings = request.app.state.settings
    plan = None
    ids = sold_prices(settings)
    if ids is not None:
        figures = getattr(request.app.state, "plan_figures", None)
        if figures is None:
            figures = request.app.state.plan_figures = PlanFigures()
        plan = await run_in_threadpool(figures.get, ids, lambda: paddle_api(request))
    return {"operator": operator(settings), "plan": plan, "writer": writer(settings)}


@router.get("/site")
async def site(request: Request) -> JSONResponse:
    """Public, no session: the operator's details, the plan's two prices and the chat's AI
    company, each or null."""
    return JSONResponse(await site_answer(request), headers=_NO_STORE)
