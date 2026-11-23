"""The public pages' server half (ADR 0045 step 8), without a database: who runs Mothli, from the
settings; the plan's two prices, read from Paddle's catalog by Mothli's own price ids only; one
public read, ``GET /api/site``, signed out; and the browser's security policy, unchanged. Paddle
is stood in for everywhere: nothing here calls it."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.hosted.paddle_api import PaddleApi
from app.hosted.site import FAILED_SECONDS, FRESH_SECONDS, PlanFigures, read_plan, sold_prices
from tests.hosted.test_billing import (
    CHECKOUT_URL,
    KEY,
    OPERATOR,
    Recorder,
    api_with,
    paddle_settings,
)
from tests.hosted.test_companion_stream_settings import _SignedIn
from tests.hosted.test_paddle_messages_postgres import MONTHLY, MYSIGNER, SECRET, YEARLY, YEARLY_NEW
from tests.hosted.test_platform_pays import _Tenant

ROOT = Path(__file__).resolve().parents[3]
BILLING_DSN = "postgresql://unused.invalid/b"
PADDLE = dict(paddle_webhook_secret=SECRET, paddle_price_monthly=MONTHLY,
              paddle_price_yearly=f"{YEARLY_NEW},{YEARLY}", hosted_billing_dsn=BILLING_DSN)
KEYED = {**PADDLE, "paddle_api_key": KEY, "paddle_checkout_url": CHECKOUT_URL, **OPERATOR}


def entity(price_id: str, amount: str = "1299", interval: str = "month", **changes) -> dict:
    """A price as Paddle's ``GET /prices/{id}`` answers it (its ``data``), trimmed to what the
    page reads and a little more."""
    data = {"id": price_id, "product_id": "pro_" + "p" * 26, "status": "active",
            "description": "stand-in", "name": "stand-in", "type": "standard",
            "billing_cycle": {"interval": interval, "frequency": 1}, "trial_period": None,
            "tax_mode": "account_setting",
            "unit_price": {"amount": amount, "currency_code": "USD"},
            "unit_price_overrides": [], "quantity": {"minimum": 1, "maximum": 1}}
    data.update(changes)
    return data


def catalog(prices: dict[str, dict]):
    """Paddle stood in: each price id answers its entity; anything else is a 404."""
    def answer(request: httpx.Request) -> httpx.Response:
        found = prices.get(request.url.path.removeprefix("/prices/"))
        if request.method != "GET" or not request.url.path.startswith("/prices/") or not found:
            return httpx.Response(404, json={"error": {"code": "entity_not_found"}})
        return httpx.Response(200, json={"data": found})
    return answer


GOOD = {MONTHLY: entity(MONTHLY, "1299", "month"), YEARLY_NEW: entity(YEARLY_NEW, "7999", "year")}


# ---- the operator's details ---------------------------------------------------------------------

def test_the_operators_details_are_plain_text_and_one_address_and_named_when_refused() -> None:
    """The break it catches: the template's placeholder, markup, a second line or something that
    is not an address published as who runs Mothli; a refusal printing the value. Spaces around
    a value are not printed."""
    settings = paddle_settings(operator_legal_name="  Example Reading Ltd ",
                               operator_country="Ireland",
                               operator_contact_email=" hello@reader.example ")
    assert settings.operator_legal_name == "Example Reading Ltd"
    assert settings.operator_contact_email == "hello@reader.example"
    assert settings.operator_published()
    assert not paddle_settings().operator_published()
    assert not paddle_settings(**{**OPERATOR, "operator_country": ""}).operator_published()
    for change, named in (
            ({"operator_legal_name": "OWNER_MUST_FILL_IN"}, "OPERATOR_LEGAL_NAME"),
            ({"operator_legal_name": "<b>Example</b>"}, "OPERATOR_LEGAL_NAME"),
            ({"operator_legal_name": "Example\nReading"}, "OPERATOR_LEGAL_NAME"),
            ({"operator_legal_name": "x" * 201}, "OPERATOR_LEGAL_NAME"),
            ({"operator_country": "owner_must_fill_in"}, "OPERATOR_COUNTRY"),
            ({"operator_country": "I"}, "OPERATOR_COUNTRY"),
            ({"operator_contact_email": "OWNER_MUST_FILL_IN"}, "OPERATOR_CONTACT_EMAIL"),
            ({"operator_contact_email": "OWNER_MUST_FILL_IN@reader.example"},
             "OPERATOR_CONTACT_EMAIL"),
            ({"operator_contact_email": "hello@reader"}, "OPERATOR_CONTACT_EMAIL"),
            ({"operator_contact_email": "hello@reader.example, b@reader.example"},
             "OPERATOR_CONTACT_EMAIL"),
            ({"operator_contact_email": "mailto:hello@reader.example"}, "OPERATOR_CONTACT_EMAIL")):
        with pytest.raises(ValidationError) as refused:
            paddle_settings(**{**OPERATOR, **change})
        assert named in str(refused.value), change
        value = next(iter(change.values()))
        assert value not in str(refused.value) or value == "I", change


def test_the_api_key_needs_the_operators_details() -> None:
    """The break it catches: a server that can take payments whose terms would not say who runs
    it. Each of the three is required with PADDLE_API_KEY; none is required without it."""
    assert paddle_settings(**KEYED).paddle_api_key is not None
    assert paddle_settings(**PADDLE).operator_published() is False
    for missing in OPERATOR:
        with pytest.raises(ValidationError) as refused:
            paddle_settings(**{**KEYED, missing: ""})
        assert "PADDLE_API_KEY requires OPERATOR_LEGAL_NAME" in str(refused.value), missing
        assert KEY not in str(refused.value)


def test_the_template_names_the_operators_details_commented_out() -> None:
    """The break it catches: the three settings missing from the deployment template, or set
    there with placeholders that a copied template would publish."""
    template = (ROOT / "deploy" / "app.env.example").read_text(encoding="utf-8")
    for setting in ("OPERATOR_LEGAL_NAME", "OPERATOR_COUNTRY", "OPERATOR_CONTACT_EMAIL"):
        assert f"\n# {setting}=OWNER_MUST_FILL_IN\n" in template, setting
        assert f"\n{setting}=" not in template, setting


# ---- the prices, from Paddle's catalog ----------------------------------------------------------

def test_a_price_is_read_by_one_well_formed_id_as_paddles_documentation_describes() -> None:
    """The break it catches: the read sent anywhere but ``GET /prices/{id}`` in the configured
    environment, without the key or Paddle's version; or a malformed id, another kind of id, a
    query or a path sent at all."""
    api, recorder = api_with(catalog(GOOD))
    assert api.price(MONTHLY) == GOOD[MONTHLY]
    request = recorder.requests[0]
    assert (request.method, str(request.url)) == (
        "GET", f"https://sandbox-api.paddle.com/prices/{MONTHLY}")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["paddle-version"] == "1" and request.content == b""
    for bad in ("", MYSIGNER + "x", "pri_short", f"{MONTHLY}?include=product", "../products",
                "sub_" + "a" * 26, None):
        with pytest.raises(ValueError):
            api.price(bad)
    assert len(recorder.requests) == 1


def test_the_figures_are_the_two_prices_sold_now_or_none_when_they_do_not_hold_together() -> None:
    """The break it catches: a figure shown that could contradict the checkout: an archived
    price, a price billed other than once a month or once a year, two currencies, an amount that
    is not a whole positive number of the smallest unit, an answer about another price; or a
    failure or refusal (a key without ``price.read``) read as a price."""
    api, _recorder = api_with(catalog(GOOD))
    assert read_plan(api, MONTHLY, YEARLY_NEW) == {"currency": "USD", "monthly": "1299",
                                                   "yearly": "7999"}
    broken = (
        {MONTHLY: entity(MONTHLY, status="archived")},
        {MONTHLY: entity(MONTHLY, interval="year")},
        {MONTHLY: entity(MONTHLY, billing_cycle={"interval": "month", "frequency": 3})},
        {MONTHLY: entity(MONTHLY, billing_cycle=None)},
        {MONTHLY: entity(MONTHLY, unit_price={"amount": "1299", "currency_code": "EUR"})},
        {MONTHLY: entity(MONTHLY, "12.99")},
        {MONTHLY: entity(MONTHLY, "0")},
        {MONTHLY: entity(MONTHLY, "-1299")},
        {MONTHLY: entity(MONTHLY, unit_price={"amount": 1299, "currency_code": "USD"})},
        {MONTHLY: entity(YEARLY)},
        {YEARLY_NEW: entity(YEARLY_NEW, interval="month")},
        {YEARLY_NEW: None},
    )
    for change in broken:
        prices = {**GOOD, **change}
        api, _recorder = api_with(catalog({k: v for k, v in prices.items() if v}))
        assert read_plan(api, MONTHLY, YEARLY_NEW) is None, change
    for answer in (lambda r: httpx.Response(403, json={"error": {"code": "forbidden"}}),
                   lambda r: httpx.Response(502, text="down"),
                   lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r))):
        api, _recorder = api_with(answer)
        assert read_plan(api, MONTHLY, YEARLY_NEW) is None


def test_only_the_prices_sold_now_are_read_and_only_with_the_key() -> None:
    """The shared Paddle account. The break it catches: an old price, the other product's or any
    id not first in Mothli's own settings asked for; a read attempted with no key or a plan
    missing."""
    assert sold_prices(paddle_settings(**KEYED)) == (MONTHLY, YEARLY_NEW)
    assert sold_prices(paddle_settings(**PADDLE)) is None
    assert sold_prices(paddle_settings()) is None
    assert sold_prices(paddle_settings(**{**KEYED, "paddle_price_monthly": ""})) is None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_figures_are_kept_for_a_while_and_a_failure_for_less() -> None:
    """The break it catches: every visit to the front page calling Paddle (a visitor could make
    the server hammer the shared account); a failure kept as long as a good answer, or a good
    answer kept for ever (a price changed in Paddle never shown)."""
    clock = Clock()
    figures = PlanFigures(clock)
    recorder = Recorder(catalog(GOOD))
    api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(recorder))
    ids = (MONTHLY, YEARLY_NEW)
    first = figures.get(ids, lambda: api)
    assert first == {"currency": "USD", "monthly": "1299", "yearly": "7999"}
    assert len(recorder.requests) == 2
    clock.now += FRESH_SECONDS - 1
    assert figures.get(ids, lambda: api) == first and len(recorder.requests) == 2
    clock.now += 1
    recorder.answer = lambda r: httpx.Response(502)
    assert figures.get(ids, lambda: api) is None
    calls = len(recorder.requests)
    clock.now += FAILED_SECONDS - 1
    assert figures.get(ids, lambda: api) is None and len(recorder.requests) == calls
    clock.now += 1
    recorder.answer = catalog(GOOD)
    assert figures.get(ids, lambda: api) == first
    assert FAILED_SECONDS < FRESH_SECONDS <= 3600


def test_a_visitor_never_waits_on_another_visitors_read() -> None:
    """The break it catches: visitors queueing behind one slow read of Paddle (each holding one
    of the server's threads for up to the client's timeout)."""
    figures = PlanFigures(Clock())
    assert figures._lock.acquire(blocking=False)
    try:
        called = []
        assert figures.get((MONTHLY, YEARLY_NEW), lambda: called.append(1)) is None
        assert called == []
    finally:
        figures._lock.release()


# ---- the public read, through the hosted app ----------------------------------------------------

def site_answer(tmp_path, answer=None, **values):
    """``GET /api/site`` through the hosted app, signed out; Paddle stood in by ``answer``.
    Returns the response and the stand-in's recorder."""
    from app.main import create_app
    from tests.hosted.test_companion_stream_settings import hosted_settings

    recorder = Recorder(answer or catalog(GOOD))
    app = create_app(hosted_settings(tmp_path, **values), auth_repository=_SignedIn(),
                     tenant_repository=_Tenant(), oidc_client=object())
    with TestClient(app, base_url="https://reader.example") as client:
        app.state.paddle_api = PaddleApi(KEY, "sandbox", transport=httpx.MockTransport(recorder))
        responses = [client.get("/api/site"), client.get("/api/site")]
    return responses, recorder


def test_the_public_read_gives_the_operator_and_the_prices_signed_out(tmp_path) -> None:
    """The break it catches: the read needing a session (the front page and the terms would be
    empty for a visitor or a reviewer); the prices not the ones Paddle's catalog holds; the
    server asking Paddle for anything but Mothli's two prices sold now, or asking again on the
    next visit; a secret, an id, the token or the billing address in the answer."""
    responses, recorder = site_answer(tmp_path, **KEYED, paddle_client_token="test_" + "s" * 20)
    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json() == {
            "operator": {"legal_name": "Example Reading Ltd", "country": "Ireland",
                         "contact_email": "hello@reader.example"},
            "plan": {"currency": "USD", "monthly": "1299", "yearly": "7999"}}
        assert "no-store" in response.headers["cache-control"]
        for secret in (KEY, SECRET, BILLING_DSN, "unused.invalid", "apikey", "ntfset", MONTHLY,
                       YEARLY_NEW, YEARLY, "test_", CHECKOUT_URL):
            assert secret not in response.text, secret
    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("GET", f"/prices/{MONTHLY}"), ("GET", f"/prices/{YEARLY_NEW}")]


def test_without_paddle_or_without_the_operator_the_read_says_so_and_calls_nothing(
        tmp_path) -> None:
    """The break it catches: a figure where Paddle is not set up, or Paddle called without a key;
    an operator published with a detail missing."""
    for values in ({}, PADDLE):
        responses, recorder = site_answer(tmp_path, **values)
        assert responses[0].json() == {"operator": None, "plan": None}, values
        assert recorder.requests == []
    responses, recorder = site_answer(tmp_path, **OPERATOR)
    assert responses[0].json()["operator"]["legal_name"] == "Example Reading Ltd"
    assert responses[0].json()["plan"] is None and recorder.requests == []


def test_paddle_unreachable_or_refusing_gives_no_figure_and_is_not_asked_again_at_once(
        tmp_path) -> None:
    """The break it catches: a failure shown as an error to a visitor, as a wrong or stale
    number, or every visit retried against Paddle."""
    for answer in (lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r)),
                   lambda r: httpx.Response(403, json={"error": {"code": "forbidden"}}),
                   lambda r: httpx.Response(503)):
        responses, recorder = site_answer(tmp_path, answer, **KEYED)
        for response in responses:
            assert response.status_code == 200
            assert response.json()["plan"] is None
            assert response.json()["operator"] is not None
        assert len(recorder.requests) == 1


# ---- the proxy and the browser's rules ----------------------------------------------------------

# The policy as it stood before step 8. The public pages need nothing more: no Paddle script, no
# analytics, no fonts or images from elsewhere.
POLICY_BEFORE_STEP_8 = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "frame-src 'self' blob: https://buy.paddle.com https://sandbox-buy.paddle.com; "
    "form-action 'self'; script-src 'self' https://cdn.paddle.com; script-src-attr 'none'; "
    "style-src 'self' 'unsafe-inline' https://cdn.paddle.com https://sandbox-cdn.paddle.com; "
    "img-src 'self' data: blob:; font-src 'self' data: blob:; media-src 'self' data: blob:; "
    "connect-src 'self' https://api.paddle.com https://sandbox-api.paddle.com; "
    "worker-src 'self' blob:")


def test_the_security_policy_is_unchanged_by_the_public_pages() -> None:
    """The break it catches: the policy widened for the public pages."""
    from app.main import _CONTENT_SECURITY_POLICY

    assert _CONTENT_SECURITY_POLICY == POLICY_BEFORE_STEP_8


def test_the_proxy_serves_the_app_at_each_public_pages_address_and_nowhere_else() -> None:
    """The break it catches: a public page's address answered 404 by the proxy (a reviewer
    following the footer finds nothing), or the app served in place of every missing file. The
    addresses are the app's own list (frontend/src/public/pages.ts)."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    matched = re.findall(r"^\t\t@pages path (.+)$", caddyfile, flags=re.M)
    assert len(matched) == 1
    assert "\t\trewrite @pages /index.html\n" in caddyfile
    assert "try_files" not in caddyfile
    pages = (ROOT / "frontend" / "src" / "public" / "pages.ts").read_text(encoding="utf-8")
    listed = set(re.findall(r'^\s+(?:\w+): "(/[a-z]+)",$', pages, flags=re.M))
    assert set(matched[0].split()) == listed == {"/terms", "/privacy", "/refunds", "/contact"}


def test_the_answer_shape_is_what_the_frontend_reads() -> None:
    """The break it catches: the server and the page disagreeing on the answer's names."""
    site_ts = (ROOT / "frontend" / "src" / "public" / "site.ts").read_text(encoding="utf-8")
    for name in ("legal_name", "country", "contact_email", "currency", "monthly", "yearly"):
        assert re.search(rf"\b{name}\b", site_ts), name
