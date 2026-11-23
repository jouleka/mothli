"""What the browser needs for the plan sheet (ADR 0045 step 4, the screens), without a database:
Paddle's public client token, its environment and the price sold for each plan, folded into the
plan the account sheet reads; never a secret. And the browser's rules for outside scripts: Paddle's
own addresses, exactly, in every place the app sets them. Nothing calls Paddle."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.hosted.auth.api import SESSION_COOKIE
from app.hosted.auth.tokens import new_token
from tests.hosted.test_billing import CHECKOUT_URL, KEY, LIVE_KEY, paddle_settings
from tests.hosted.test_companion_stream_settings import _SignedIn
from tests.hosted.test_paddle_messages_postgres import MONTHLY, SECRET, YEARLY, YEARLY_NEW
from tests.hosted.test_platform_pays import _Tenant

ROOT = Path(__file__).resolve().parents[3]

# Stand-ins in the shape of Paddle's client-side tokens, built in pieces: not tokens.
TOKEN = "_".join(("test", "clientstandin" * 2))
LIVE_TOKEN = "_".join(("live", "clientstandin" * 2))
BILLING_DSN = "postgresql://unused.invalid/b"
PADDLE = dict(paddle_webhook_secret=SECRET, paddle_price_monthly=MONTHLY,
              paddle_price_yearly=f"{YEARLY_NEW},{YEARLY}", hosted_billing_dsn=BILLING_DSN)
# Paddle's messages and its API: the key comes with Mothli's own payment-link page.
KEYED = {**PADDLE, "paddle_api_key": KEY, "paddle_checkout_url": CHECKOUT_URL}


def test_the_client_token_is_checked_loosely_against_its_environment() -> None:
    """The break it catches: a real token refused over a character a guessed pattern left out;
    a sandbox token used with live Paddle or the reverse; a placeholder or a token with a space
    accepted; a token set with no checkout route to use it. A refusal names the setting."""
    assert paddle_settings(**KEYED).paddle_client_token == ""
    assert paddle_settings(**KEYED, paddle_client_token=TOKEN).paddle_client_token == TOKEN
    live = {**KEYED, "paddle_environment": "production", "paddle_api_key": LIVE_KEY}
    assert paddle_settings(**live, paddle_client_token=LIVE_TOKEN).paddle_client_token \
        == LIVE_TOKEN
    for values in ({**KEYED, "paddle_client_token": LIVE_TOKEN},
                   {**live, "paddle_client_token": TOKEN},
                   {**KEYED, "paddle_client_token": "OWNER_MUST_FILL_IN"},
                   {**KEYED, "paddle_client_token": TOKEN.replace("client", "client ", 1)},
                   {**KEYED, "paddle_client_token": TOKEN + "\n"},
                   {**KEYED, "paddle_client_token": "test_"},
                   {**PADDLE, "paddle_client_token": TOKEN}):
        with pytest.raises(ValidationError) as refused:
            paddle_settings(**values)
        assert "PADDLE_CLIENT_TOKEN" in str(refused.value), values
        assert TOKEN not in str(refused.value) and LIVE_TOKEN not in str(refused.value)


def test_the_checkout_setup_is_the_token_the_environment_and_each_plans_current_price() -> None:
    """The break it catches: an old price offered (only the first id in a setting is sold); a
    plan with no price offered; the setup given where checkout cannot work (no API key, so no
    checkout route, or no client token)."""
    from app.hosted.subscribe import checkout_setup

    assert checkout_setup(paddle_settings(**KEYED, paddle_client_token=TOKEN)) == {
        "environment": "sandbox", "client_token": TOKEN,
        "prices": {"monthly": MONTHLY, "yearly": YEARLY_NEW}}
    only_yearly = {**KEYED, "paddle_price_monthly": ""}
    assert checkout_setup(paddle_settings(**only_yearly, paddle_client_token=TOKEN))["prices"] \
        == {"monthly": None, "yearly": YEARLY_NEW}
    assert checkout_setup(paddle_settings(**KEYED)) is None
    assert checkout_setup(paddle_settings(**PADDLE)) is None
    assert checkout_setup(paddle_settings()) is None


ROW = {"state": "free", "why": None, "plan": None, "next_date": "2026-12-04T10:00:00+00:00",
       "subscribed": False, "cancel_scheduled": False, "payment_retrying": False}


class _Readers(_Tenant):
    async def consume_request_limit(self, owner) -> None:
        return None


def plan_answer(tmp_path, monkeypatch, **values) -> tuple[dict, str]:
    """``GET /api/billing/plan`` through the hosted app, the plan itself stood in (it is read
    from PostgreSQL: tests/hosted/test_paddle_subscribe_postgres.py)."""
    from app.hosted import subscribe
    from app.main import create_app
    from tests.hosted.test_companion_stream_settings import hosted_settings

    monkeypatch.setattr(subscribe, "read_plan", lambda settings, owner: dict(ROW))
    app = create_app(hosted_settings(tmp_path, **values), auth_repository=_SignedIn(),
                     tenant_repository=_Readers(), oidc_client=object())
    with TestClient(app, base_url="https://reader.example") as client:
        client.cookies.set(SESSION_COOKIE, new_token())
        response = client.get("/api/billing/plan")
        assert response.status_code == 200, response.text
        assert "no-store" in response.headers["cache-control"]
        return response.json(), response.text


def test_the_plan_carries_what_the_browser_needs_and_no_secret(tmp_path, monkeypatch) -> None:
    """The break it catches: the API key, the webhook secret or the billing address reaching the
    browser in the plan; the setup missing where checkout works; the sheet told it may cancel,
    keep or manage where those routes do not exist."""
    answer, text = plan_answer(tmp_path, monkeypatch, **KEYED, paddle_client_token=TOKEN)
    assert answer == {**ROW, "manage": True, "checkout": {
        "environment": "sandbox", "client_token": TOKEN,
        "prices": {"monthly": MONTHLY, "yearly": YEARLY_NEW}}}
    for secret in (KEY, SECRET, BILLING_DSN, "apikey", "ntfset", "unused.invalid"):
        assert secret not in text, secret


@pytest.mark.parametrize("values", [{}, PADDLE, KEYED],
                         ids=["no-paddle", "messages-only", "no-client-token"])
def test_a_server_without_checkout_says_so_and_holds_no_secret(values, tmp_path,
                                                               monkeypatch) -> None:
    """The break it catches: a "See the plan" or a cancel button that leads to a route this
    server does not have."""
    answer, text = plan_answer(tmp_path, monkeypatch, **values)
    assert answer["checkout"] is None
    assert answer["manage"] is ("paddle_api_key" in values)
    assert KEY not in text and SECRET not in text and "unused.invalid" not in text


def payment_link_answer(tmp_path, **values):
    """``GET /api/billing/payment-link`` through the hosted app, signed out (no session)."""
    from app.main import create_app
    from tests.hosted.test_companion_stream_settings import hosted_settings

    app = create_app(hosted_settings(tmp_path, **values), auth_repository=_SignedIn(),
                     tenant_repository=_Readers(), oidc_client=object())
    with TestClient(app, base_url="https://reader.example") as client:
        return client.get("/api/billing/payment-link")


def test_the_payment_link_page_reads_the_token_and_environment_and_nothing_else(
        tmp_path) -> None:
    """Mothli's payment-link page, which a visitor arriving from Paddle's email may open signed
    out. The break it catches: the read needing a session (the visitor could not pay); answering
    more than the two values Paddle.js needs (a price, the address, the API key, the webhook
    secret, the billing role's address); cached; or answering where no checkout opens."""
    response = payment_link_answer(tmp_path, **KEYED, paddle_client_token=TOKEN)
    assert response.status_code == 200, response.text
    assert response.json() == {"environment": "sandbox", "client_token": TOKEN}
    assert "no-store" in response.headers["cache-control"]
    for secret in (KEY, SECRET, BILLING_DSN, "apikey", "ntfset", "unused.invalid", MONTHLY,
                   YEARLY_NEW, CHECKOUT_URL, "reader.example"):
        assert secret not in response.text, secret
    live = payment_link_answer(tmp_path, **{**KEYED, "paddle_environment": "production",
                                            "paddle_api_key": LIVE_KEY},
                               paddle_client_token=LIVE_TOKEN)
    assert live.json() == {"environment": "production", "client_token": LIVE_TOKEN}
    assert LIVE_KEY not in live.text
    for values in ({}, PADDLE, KEYED):
        refused = payment_link_answer(tmp_path, **values)
        assert refused.status_code == 404, values
        assert KEY not in refused.text and SECRET not in refused.text


# ---- the browser's rules for outside scripts ------------------------------------------------------

# Paddle's own addresses, read from Paddle.js 2.13.0 itself (ADR 0045, "Step 4, screens, built",
# says how): its script, the stylesheet it adds, its checkout window and the price preview it asks
# for, for live Paddle and the sandbox.
PADDLE_SOURCES = {
    "script-src": {"https://cdn.paddle.com"},
    "style-src": {"https://cdn.paddle.com", "https://sandbox-cdn.paddle.com"},
    "frame-src": {"https://buy.paddle.com", "https://sandbox-buy.paddle.com"},
    "connect-src": {"https://api.paddle.com", "https://sandbox-api.paddle.com"},
}


def directives(policy: str) -> dict[str, list[str]]:
    parsed = {}
    for part in policy.split(";"):
        name, *sources = part.split()
        parsed[name] = sources
    return parsed


def outside(policy: str) -> dict[str, set[str]]:
    """Every source of the policy that is not the app itself, by directive."""
    return {name: found for name, sources in directives(policy).items()
            if (found := {source for source in sources if "://" in source or "." in source})}


def test_the_policy_allows_paddles_own_addresses_and_nothing_wider() -> None:
    """The break it catches: Paddle's checkout blocked by the policy (no subscribing at all); or
    the policy opened wider than Paddle's own addresses, a wildcard, or Paddle's addresses where
    they are not needed (no script from Paddle's checkout window or price address, no
    ProfitWell: Mothli does not use Paddle Retain)."""
    from app.main import _CONTENT_SECURITY_POLICY

    assert outside(_CONTENT_SECURITY_POLICY) == PADDLE_SOURCES
    assert "*" not in _CONTENT_SECURITY_POLICY
    parsed = directives(_CONTENT_SECURITY_POLICY)
    assert parsed["script-src"] == ["'self'", "https://cdn.paddle.com"]
    assert parsed["script-src-attr"] == ["'none'"]
    assert parsed["frame-ancestors"] == ["'none'"]
    assert parsed["default-src"] == ["'self'"]


def test_the_dev_server_sets_the_backends_policy_with_only_its_own_additions() -> None:
    """The break it catches: Paddle allowed in production but blocked under ``npm run dev`` (or
    the reverse), or the dev server's policy drifting from the backend's in any other way than
    its hot-reload socket and preamble."""
    from app.main import _CONTENT_SECURITY_POLICY

    config = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    block = config.split('"Content-Security-Policy": [', 1)[1].split("].join", 1)[0]
    dev = "; ".join(re.findall(r'^\s+"(.+)",$', block, flags=re.M))
    assert dev.replace("connect-src 'self' ws: ", "connect-src 'self' ") == \
        _CONTENT_SECURITY_POLICY


def test_the_plan_sheet_is_the_only_place_paddles_script_is_named() -> None:
    """The break it catches: Paddle's script put in the page's head, loaded on every screen."""
    named = sorted(path.relative_to(ROOT).as_posix()
                   for path in (ROOT / "frontend").glob("**/*")
                   if path.is_file() and path.suffix in {".ts", ".tsx", ".html"}
                   and "node_modules" not in path.parts and "dist" not in path.parts
                   and "cdn.paddle.com/paddle" in path.read_text(encoding="utf-8",
                                                                  errors="ignore"))
    assert named == ["frontend/src/shelf/paddle.test.ts", "frontend/src/shelf/paddle.ts"]
