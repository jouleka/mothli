"""Paddle's messages without a database (ADR 0045 step 3): the signature check, which messages
are Mothli's, and the settings. Messages are built and signed here; nothing calls Paddle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.hosted import paddle_api
from app.hosted.billing import Malformed, read_message, read_subscription, verify_signature
from app.hosted.paddle_api import PaddleApi, PaddleRefused, PaddleUnavailable
from tests.hosted.test_paddle_messages_postgres import (
    MONTHLY,
    MYSIGNER,
    SECRET,
    YEARLY,
    YEARLY_NEW,
    message,
    paddle_id,
    sub_id,
)

PRICES = frozenset({MONTHLY, YEARLY, YEARLY_NEW})
NOW = 1_800_000_000


def signed(body: bytes, ts: int = NOW, secret: str = SECRET) -> str:
    return "ts={};h1={}".format(
        ts, hmac.new(secret.encode(), f"{ts}:".encode() + body, hashlib.sha256).hexdigest())


def test_the_signature_is_hmac_sha256_of_the_time_a_colon_and_the_raw_body() -> None:
    """The break it catches: a check that hashes anything but ``ts:body`` exactly, accepts a
    stale or future time, accepts an empty secret, or needs the first ``h1`` to match."""
    body = b'{"event_id": "evt_x"}'
    header = signed(body)
    assert verify_signature(header, body, SECRET, NOW, 300)
    assert verify_signature(header, body, SECRET, NOW + 300, 300)
    assert not verify_signature(header, body, SECRET, NOW + 301, 300)
    assert not verify_signature(header, body, SECRET, NOW - 301, 300)
    assert not verify_signature(header, body + b" ", SECRET, NOW, 300)
    assert not verify_signature(header, body, SECRET + "x", NOW, 300)
    assert not verify_signature(signed(body, secret=""), body, "", NOW, 300)
    digest = header.split("h1=")[1]
    assert verify_signature(f"ts={NOW};h1={'0' * 64};h1={digest}", body, SECRET, NOW, 300)
    assert verify_signature(f"ts={NOW}; h1={digest}", body, SECRET, NOW, 300)
    for broken in (None, "", f"h1={digest}", f"ts={NOW}", f"ts=-{NOW};h1={digest}",
                   f"ts={NOW}.5;h1={digest}", f"ts={NOW};h1=é{digest}", f"ts={NOW};v1={digest}",
                   f"ts={'9' * 40};h1={digest}"):
        assert not verify_signature(broken, body, SECRET, NOW, 300), broken


def test_only_a_subscription_with_every_item_one_of_mothlis_prices_is_mothlis() -> None:
    """The break it catches: the shared Paddle account's other product read as Mothli's because
    its custom data names a reader or says app=mothli, or because one of its items is."""
    reader = uuid.uuid4()
    read = read_message(message(sub_id("a"), reader=reader, prices=(YEARLY,)), PRICES)
    assert (read.subscription_id, read.status, read.named_reader) == (sub_id("a"), "active",
                                                                       reader)
    for payload in (message(sub_id("a"), reader=reader, prices=(MYSIGNER,)),
                    message(sub_id("a"), reader=reader, prices=(MONTHLY, MYSIGNER)),
                    message(sub_id("a"), reader=reader, prices=()),
                    message(sub_id("a"), reader=reader, event_type="transaction.completed"),
                    message(sub_id("a"), custom={"mothli_reader": str(reader), "app": "mothli"},
                            prices=(MYSIGNER,)),
                    [], "text", {}, {"event_type": "subscription.updated", "data": []}):
        assert read_message(payload, PRICES) is None
    # No Mothli price configured: nothing is Mothli's.
    assert read_message(message(sub_id("a"), reader=reader), frozenset()) is None
    # Malformed only once it is known to be Mothli's.
    broken = message(sub_id("a"), "frozen", reader=reader)
    with pytest.raises(Malformed):
        read_message(broken, PRICES)
    broken["data"]["items"][0]["price"]["id"] = MYSIGNER
    assert read_message(broken, PRICES) is None


def test_a_scheduled_cancel_or_pause_is_the_plans_end_and_a_resume_is_not() -> None:
    later = "2026-12-23T10:00:00.123456Z"
    for action, ends in (("cancel", True), ("pause", True), ("resume", False)):
        read = read_message(message(sub_id("a"), scheduled={
            "action": action, "effective_at": later, "resume_at": None}), PRICES)
        assert (read.ends_at is not None) == ends, action
    assert read_message(message(sub_id("a")), PRICES).ends_at is None


def paddle_settings(**values) -> Settings:
    base = dict(
        _env_file=None, deployment_mode="hosted", hosted_auth_dsn="postgresql://unused.invalid/a",
        hosted_tenant_dsn="postgresql://unused.invalid/t", oidc_issuer="https://idp.example",
        oidc_client_id="mothli", oidc_client_secret="stand-in",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
        trusted_hosts="reader.example", hosted_storage_backend="filesystem",
        hosted_storage_filesystem_root="/tmp/books",
        hosted_storage_filesystem_key=base64.b64encode(b"k" * 32).decode("ascii"))
    return Settings(**{**base, **values})


def test_the_paddle_settings_are_checked_like_their_neighbours() -> None:
    """The break it catches: a server taking Paddle's messages with no billing role, no price to
    tell Mothli's from the other product's, a placeholder for a secret or a price, or the same id
    as both plans; or local mode with a secret. A refusal names the setting, never its value."""
    good = dict(paddle_webhook_secret=SECRET, paddle_price_monthly=MONTHLY,
                paddle_price_yearly=f" {YEARLY_NEW} , {YEARLY} ",
                hosted_billing_dsn="postgresql://unused.invalid/b")
    settings = paddle_settings(**good)
    assert settings.paddle_prices() == PRICES
    assert settings.paddle_environment == "sandbox"
    assert settings.paddle_webhook_tolerance_seconds == 300
    assert paddle_settings().paddle_webhook_secret is None
    assert paddle_settings(paddle_webhook_secret="").paddle_webhook_secret is None
    for change, named in (
            ({"hosted_billing_dsn": None}, "HOSTED_BILLING_DSN"),
            ({"paddle_price_monthly": "", "paddle_price_yearly": ""}, "PADDLE_PRICE_MONTHLY"),
            ({"paddle_webhook_secret": "OWNER_MUST_FILL_IN"}, "PADDLE_WEBHOOK_SECRET"),
            ({"paddle_price_monthly": "OWNER_MUST_FILL_IN"}, "PADDLE_PRICE_MONTHLY"),
            ({"paddle_price_yearly": f"{YEARLY},pri_short"}, "PADDLE_PRICE_YEARLY"),
            ({"paddle_price_yearly": MONTHLY}, "share a price id"),
            ({"paddle_environment": "live"}, "paddle_environment"),
            ({"paddle_webhook_tolerance_seconds": 4}, "paddle_webhook_tolerance_seconds"),
            ({"deployment_mode": "local"}, "hosted-mode setting")):
        with pytest.raises(ValidationError) as refused:
            paddle_settings(**{**good, **change})
        assert named in str(refused.value), change
        assert SECRET not in str(refused.value)
    assert paddle_id("pri_", "x") not in PRICES


def test_a_real_looking_secret_key_with_base64_characters_is_accepted_and_checks() -> None:
    """The break it catches: the owner pasting Paddle's real secret key and the server refusing
    to start because the key holds a character a guessed pattern left out. Paddle's own SDK
    example key holds a "+"; its second part is base64-like. Only the prefix, no whitespace and
    a sane length are required. (Built in pieces: a stand-in, not a key.)"""
    key = "pdl_ntfset_" + "01hs0t3tw21j988db1pam5xg8m" + "_" + "Gr/OW+LNef=" * 3
    for extra in ("/", "+", "="):
        assert extra in key
    settings = paddle_settings(paddle_webhook_secret=key, paddle_price_monthly=MONTHLY,
                               hosted_billing_dsn="postgresql://unused.invalid/b")
    assert settings.paddle_webhook_secret.get_secret_value() == key
    body = b'{"event_id": "evt_x"}'
    assert verify_signature(signed(body, secret=key), body, key, NOW, 300)
    for refused in ("pdl_ntfset_", "pdl_ntfset_short", key.replace("/", " "), key + "\n",
                    "x" + key, "pdl_ntfset_" + "a" * 501):
        with pytest.raises(ValidationError):
            paddle_settings(paddle_webhook_secret=refused, paddle_price_monthly=MONTHLY,
                            hosted_billing_dsn="postgresql://unused.invalid/b")


def test_back_on_names_a_day_begun_nowhere_before_the_moment() -> None:
    """The break it catches: Mothli naming a day it is back that has already begun somewhere on
    Earth before it is (a reader in UTC+14 would ask and be stopped), or one later than needed
    when the moment is a midnight there."""
    from datetime import UTC, date, datetime, timedelta

    from app.hosted.companion.spend import back_on, first_month_line

    for moment, day in ((datetime(2026, 12, 20, 14, tzinfo=UTC), date(2026, 12, 22)),
                        (datetime(2026, 12, 20, 9, tzinfo=UTC), date(2026, 12, 21)),
                        (datetime(2026, 12, 20, 10, tzinfo=UTC), date(2026, 12, 21)),
                        (datetime(2026, 12, 31, 12, tzinfo=UTC), date(2027, 1, 2))):
        assert back_on(moment) == day, moment
        begins_first = datetime.combine(day, datetime.min.time(), UTC) - timedelta(hours=14)
        assert begins_first >= moment
        assert begins_first - timedelta(days=1) < moment     # and no later than needed
    line = first_month_line(date(2026, 12, 21))
    assert line == ("We’ve talked our way through your first month together already! I’ll be "
                    "back on 21 December. Your book and your place are right here.")
    assert "unlimited" not in line.casefold() and "question" not in line.casefold()


def test_the_first_paid_month_is_the_owners_number_and_reaches_the_spend_check() -> None:
    """The break it catches: the first paid month's ceiling not the owner's $6.30 (about 300
    replies at 2.1 cents), not passed to the spend check, or accepting a negative amount."""
    from decimal import Decimal

    from app.hosted.companion.spend import SpendLimits, set_spend_limits, spend_limits

    assert paddle_settings().hosted_first_paid_month_usd == 6.30
    assert SpendLimits().first_paid_month_usd == Decimal("6.30")
    assert round(Decimal("6.30") / Decimal("0.021")) == 300
    before = spend_limits()
    try:
        set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=4, platform_usd_per_day=15,
                         first_paid_month_usd=Decimal("7.5"))
        assert spend_limits().first_paid_month_usd == Decimal("7.5")
        with pytest.raises(ValueError):
            set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=4,
                             platform_usd_per_day=15, first_paid_month_usd=-1)
    finally:
        set_spend_limits(**{name: getattr(before, name) for name in (
            "reader_usd_per_day", "reader_usd_per_month", "platform_usd_per_day",
            "notes_usd_per_day", "background_usd_per_day", "reader_background_usd_per_day",
            "reader_background_usd_per_month", "first_paid_month_usd")})
    with pytest.raises(ValidationError):
        paddle_settings(hosted_first_paid_month_usd=-0.01)


# ---- step 4: Paddle's API, without a database ----------------------------------------------------

# A stand-in in the shape of a sandbox API key, built in pieces: not a key.
KEY = "_".join(("pdl", "sdbx", "apikey", "teststandin" * 4))
LIVE_KEY = "_".join(("pdl", "live", "apikey", "teststandin" * 4))
LIVE = {"paddle_environment": "production"}
# Mothli's own page for Paddle's payment links, on the test app's host.
CHECKOUT_URL = "https://reader.example/"
# Who runs this stand-in Mothli (ADR 0045 step 8): required with the API key.
OPERATOR = {"operator_legal_name": "Example Reading Ltd", "operator_country": "Ireland",
            "operator_contact_email": "hello@reader.example"}


class Recorder:
    """Paddle stood in: every request kept, each answered by ``answer``."""

    def __init__(self, answer) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


def api_with(answer, environment: str = "sandbox") -> tuple[PaddleApi, Recorder]:
    recorder = Recorder(answer)
    return PaddleApi(KEY, environment, transport=httpx.MockTransport(recorder)), recorder


def subscription_entity(sub: str = sub_id("a"), **changes) -> dict:
    data = message(sub)["data"]
    data["updated_at"] = "2026-11-23T10:00:00.000000Z"
    data.update(changes)
    return data


def test_each_paddle_call_is_sent_as_paddles_documentation_describes_it() -> None:
    """The break it catches: a call sent to the wrong address, method or environment, without
    the bearer key or Paddle's version, with a body other than the documented one, or a cancel
    that is not at the end of the billing period."""
    entity = subscription_entity()
    portal = {"id": "cpls_x", "urls": {"general": {"overview": "https://p.example/o"},
                                       "subscriptions": []}}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions":
            return httpx.Response(201, json={"data": {"id": paddle_id("txn_", "t")}})
        if request.url.path.endswith("/portal-sessions"):
            return httpx.Response(201, json={"data": portal})
        return httpx.Response(200, json={"data": entity})

    api, recorder = api_with(answer)
    custom = {"mothli_reader": str(uuid.uuid4()), "mothli_checkout": "t" * 43}
    assert api.create_transaction(MONTHLY, custom, CHECKOUT_URL) == paddle_id("txn_", "t")
    assert api.subscription(sub_id("a")) == entity
    assert api.cancel(sub_id("a")) == entity
    assert api.remove_scheduled_change(sub_id("a")) == entity
    assert api.portal(paddle_id("ctm_", "c"), sub_id("a")) == portal["urls"]
    sent = [(r.method, str(r.url), json.loads(r.content) if r.content else None)
            for r in recorder.requests]
    base = "https://sandbox-api.paddle.com"
    assert sent == [
        ("POST", f"{base}/transactions",
         {"items": [{"price_id": MONTHLY, "quantity": 1}], "custom_data": custom,
          "checkout": {"url": CHECKOUT_URL}}),
        ("GET", f"{base}/subscriptions/{sub_id('a')}", None),
        ("POST", f"{base}/subscriptions/{sub_id('a')}/cancel",
         {"effective_from": "next_billing_period"}),
        ("PATCH", f"{base}/subscriptions/{sub_id('a')}", {"scheduled_change": None}),
        ("POST", f"{base}/customers/{paddle_id('ctm_', 'c')}/portal-sessions",
         {"subscription_ids": [sub_id("a")]}),
    ]
    for request in recorder.requests:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["paddle-version"] == "1"
    live, recorder = api_with(answer, "production")
    live.subscription(sub_id("a"))
    assert str(recorder.requests[0].url).startswith("https://api.paddle.com/")


def test_the_paddle_client_cannot_list_search_or_name_anything_but_one_well_formed_id() -> None:
    """The shared Paddle account (it also sells MySigner). The break it catches: a client able
    to list or search customers, subscriptions or transactions, make or look up a customer, or
    put anything but one Paddle id of the expected kind in a path (``../``, a query, another
    kind of id, a list)."""
    public = {name for name in dir(PaddleApi) if not name.startswith("_")}
    assert public == {"create_transaction", "price", "subscription", "cancel",
                      "remove_scheduled_change", "portal", "close"}
    api, recorder = api_with(lambda request: httpx.Response(500))
    for call in (lambda: api.subscription("sub_x/../../customers"),
                 lambda: api.subscription(paddle_id("ctm_", "c")),
                 lambda: api.subscription(sub_id("a") + "?status=active"),
                 lambda: api.cancel(""),
                 lambda: api.remove_scheduled_change(paddle_id("txn_", "t")),
                 lambda: api.portal(sub_id("a"), sub_id("a")),
                 lambda: api.portal(paddle_id("ctm_", "c"), "*"),
                 lambda: api.create_transaction(MYSIGNER + "x", {}, CHECKOUT_URL),
                 lambda: api.create_transaction(sub_id("a"), {}, CHECKOUT_URL),
                 lambda: api.create_transaction(MONTHLY, {}, "http://reader.example/"),
                 lambda: api.create_transaction(MONTHLY, {}, None)):
        with pytest.raises(ValueError):
            call()
    assert recorder.requests == []


def test_paddle_failures_are_named_by_kind_sent_once_and_logged_without_anything_of_theirs(
        caplog) -> None:
    """The break it catches: a slow or unreachable Paddle hanging the request or retried (a
    retried create or cancel could happen twice), an overloaded or failing Paddle read as a
    refusal, a refusal losing Paddle's code (the 30-minute rule needs it), an answer that is not
    Paddle's read as one; or the key, a body, an id or Paddle's words in a log line or an
    error's text."""
    caplog.set_level(logging.DEBUG)
    cases = [
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("stand-in slow", request=r)), "timeout"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("stand-in unreachable", request=r)),
         "unreachable"),
        (lambda r: httpx.Response(429, json={"error": {"code": "too_many_requests"}}), "server"),
        (lambda r: httpx.Response(502, text="bad gateway"), "server"),
        (lambda r: httpx.Response(200, text="<html>"), "unreadable"),
        (lambda r: httpx.Response(200, json={"data": []}), "unreadable"),
        (lambda r: httpx.Response(302, headers={"location": "https://elsewhere.example"}),
         "unreadable"),
    ]
    for answer, kind in cases:
        api, recorder = api_with(answer)
        with pytest.raises(PaddleUnavailable) as failed:
            api.cancel(sub_id("a"))
        assert failed.value.kind == kind and str(failed.value) == kind
        assert len(recorder.requests) == 1
    api, recorder = api_with(lambda r: httpx.Response(409, json={"error": {
        "type": "request_error", "code": "subscription_locked_renewal",
        "detail": "secret detail words", "documentation_url": "https://developer.paddle.com"}}))
    with pytest.raises(PaddleRefused) as refused:
        api.cancel(sub_id("a"))
    assert (refused.value.status, refused.value.code) == (409, "subscription_locked_renewal")
    assert len(recorder.requests) == 1
    api, _recorder = api_with(lambda r: httpx.Response(400, json={"error": {"code": "Bad Code!"}}))
    with pytest.raises(PaddleRefused) as refused:
        api.subscription(sub_id("a"))
    assert refused.value.code == "unknown"
    # A created transaction whose id is not a transaction's is not trusted.
    api, _recorder = api_with(lambda r: httpx.Response(201, json={"data": {"id": "sub_x"}}))
    with pytest.raises(PaddleUnavailable):
        api.create_transaction(MONTHLY, {}, CHECKOUT_URL)
    for secret in (KEY, sub_id("a"), "secret detail words", "bad gateway", "stand-in",
                   "elsewhere", "Bad Code"):
        assert secret not in caplog.text, secret
    assert "paddle call failed: timeout" in caplog.text
    assert "paddle call refused" in caplog.text
    assert paddle_api.TIMEOUT.connect <= 5 and paddle_api.TIMEOUT.read <= 15


def test_the_api_key_is_checked_loosely_against_its_environment_and_needs_the_messages() -> None:
    """The break it catches: the server refusing a real key over a character a guessed pattern
    left out; a live key used against the sandbox address or the reverse; the key set without
    Paddle's messages (a payment would make no one paying); a placeholder accepted. A refusal
    names the setting, never the key."""
    good = dict(paddle_webhook_secret=SECRET, paddle_price_monthly=MONTHLY,
                paddle_price_yearly=f"{YEARLY_NEW},{YEARLY}",
                hosted_billing_dsn="postgresql://unused.invalid/b", **OPERATOR)
    assert paddle_settings(**good).paddle_api_key is None
    assert paddle_settings(**good, paddle_api_key="").paddle_api_key is None
    good["paddle_checkout_url"] = CHECKOUT_URL
    real_looking = KEY[:-8] + "+/=Ab09_"
    assert paddle_settings(**good, paddle_api_key=real_looking) \
        .paddle_api_key.get_secret_value() == real_looking
    live = {**good, **LIVE}
    live["paddle_api_key"] = LIVE_KEY
    assert paddle_settings(**live).paddle_api_key.get_secret_value() == LIVE_KEY
    for change in ({"paddle_api_key": LIVE_KEY},
                   {**LIVE, "paddle_api_key": KEY},
                   {"paddle_api_key": "OWNER_MUST_FILL_IN"},
                   {"paddle_api_key": KEY.replace("teststandin", "test standin", 1)},
                   {"paddle_api_key": KEY + "\n"},
                   {"paddle_api_key": "pdl_sdbx_apikey_short"},
                   {"paddle_api_key": KEY, "paddle_webhook_secret": None}):
        with pytest.raises(ValidationError) as refused:
            paddle_settings(**{**good, **change})
        assert "PADDLE_API_KEY" in str(refused.value), change
        assert KEY not in str(refused.value) and LIVE_KEY not in str(refused.value)


def test_the_checkout_address_comes_with_the_key_and_is_this_apps_own_front_page() -> None:
    """One Paddle account, several products. The break it catches: a server with the API key and
    no address of its own starting anyway, so every checkout's emails send the buyer to the
    account's default payment link, the other product's page; an address on another host, over
    plain http, with a path the proxy does not serve, a query or fragment Paddle's own ``_ptxn``
    would collide with, a port or user information; a placeholder accepted; the address set with
    no checkout to use it. A refusal names the setting, never the key."""
    keyed = dict(paddle_webhook_secret=SECRET, paddle_price_monthly=MONTHLY,
                 hosted_billing_dsn="postgresql://unused.invalid/b", paddle_api_key=KEY,
                 **OPERATOR)
    with pytest.raises(ValidationError) as refused:
        paddle_settings(**keyed)
    assert "PADDLE_CHECKOUT_URL" in str(refused.value) and KEY not in str(refused.value)
    with pytest.raises(ValidationError) as refused:
        paddle_settings(**{**keyed, "paddle_api_key": None}, paddle_checkout_url=CHECKOUT_URL)
    assert "PADDLE_CHECKOUT_URL requires PADDLE_API_KEY" in str(refused.value)
    for address in (CHECKOUT_URL, "https://reader.example", "https://READER.example/"):
        assert paddle_settings(**keyed, paddle_checkout_url=address).paddle_checkout_url \
            == address
    two_hosts = paddle_settings(**keyed, trusted_hosts="reader.example,www.reader.example",
                                paddle_checkout_url="https://www.reader.example/")
    assert two_hosts.paddle_checkout_url == "https://www.reader.example/"
    for address in ("http://reader.example/", "https://elsewhere.example/",
                    "https://reader.example.elsewhere.example/", "https://reader.example/pay",
                    "https://reader.example/?pay=1", "https://reader.example/?",
                    "https://reader.example/#pay", "https://someone@reader.example/",
                    "https://reader.example:8443/", " https://reader.example/",
                    "OWNER_MUST_FILL_IN", "https://OWNER_MUST_FILL_IN/", "reader.example"):
        with pytest.raises(ValidationError) as refused:
            paddle_settings(**keyed, paddle_checkout_url=address)
        assert "PADDLE_CHECKOUT_URL" in str(refused.value), address
        assert KEY not in str(refused.value)


def test_paddle_refusing_the_checkout_address_tells_the_operator_in_one_fixed_line(
        caplog) -> None:
    """The domain of PADDLE_CHECKOUT_URL not yet approved in Paddle (Website approval). The break
    it catches: the refusal read as anything but a refusal (the route then answers the reader's
    kind "can't reach our payment service"), retried, or logged with the address, the key or
    Paddle's words instead of the one line that says what to do; that line written for any other
    refusal."""
    caplog.set_level(logging.DEBUG)
    api, recorder = api_with(lambda r: httpx.Response(400, json={"error": {
        "type": "request_error", "code": "transaction_checkout_url_domain_is_not_approved",
        "detail": "stand-in words about reader.example"}}))
    with pytest.raises(PaddleRefused) as refused:
        api.create_transaction(MONTHLY, {}, CHECKOUT_URL)
    assert (refused.value.status, refused.value.code) == (
        400, "transaction_checkout_url_domain_is_not_approved")
    assert len(recorder.requests) == 1
    lines = [record.getMessage() for record in caplog.records]
    assert lines == ["paddle call refused", "paddle refused the checkout address: approve the "
                     "domain of PADDLE_CHECKOUT_URL in Paddle under Website approval"]
    for secret in (KEY, "reader.example", "stand-in"):
        assert secret not in caplog.text, secret
    caplog.clear()
    api, _recorder = api_with(lambda r: httpx.Response(400, json={"error": {
        "code": "transaction_price_not_found"}}))
    with pytest.raises(PaddleRefused):
        api.create_transaction(MONTHLY, {}, CHECKOUT_URL)
    assert "checkout address" not in caplog.text


def test_a_subscription_from_paddles_api_is_read_as_a_message_is() -> None:
    """The break it catches: the regular check or a cancel's answer applying a subscription about
    another product's price, read with another time than its own ``updated_at``, or a checkout
    token taken from anything but its own custom data field in the shape the server makes."""
    reader = uuid.uuid4()
    token = "A" * 21 + "-_" + "z" * 20
    entity = subscription_entity(custom_data={"mothli_reader": str(reader),
                                              "mothli_checkout": token},
                                 items=[{"price": {"id": YEARLY_NEW}}])
    read = read_subscription(entity, PRICES)
    assert (read.event_id, read.event_type, read.named_reader, read.checkout, read.price_id) == (
        None, None, reader, token, YEARLY_NEW)
    assert read.occurred_at.isoformat() == "2026-11-23T10:00:00+00:00"
    assert read_subscription(subscription_entity(items=[{"price": {"id": MYSIGNER}}]),
                             PRICES) is None
    assert read_subscription(entity, frozenset()) is None
    with pytest.raises(Malformed):
        read_subscription(subscription_entity(updated_at=None), PRICES)
    for wrong in (None, 7, "short", token + "x", token[:-1] + "!", ["list"]):
        entity["custom_data"]["mothli_checkout"] = wrong
        assert read_subscription(entity, PRICES).checkout is None, wrong
    payload = message(sub_id("a"), reader=reader)
    assert read_message(payload, PRICES).checkout == payload["data"]["custom_data"][
        "mothli_checkout"]


def plan_row(**values) -> dict:
    row = dict(state="new", why=None, paid_by=None, gift_until=None,
               paddle_status=None, period_ends_at=None, plan_ends_at=None, paddle_price_id=None)
    row.update(values)
    return row


def test_the_plan_the_account_sheet_reads() -> None:
    """The break it catches: a gift shown with the renewal of a Paddle plan under it, a plan
    shown as monthly when it is yearly (or an old yearly price not recognised), a cancel or a
    retried payment not said, a plan past its scheduled end still offered to cancel, or an
    amount, a count or a countdown in the answer."""
    from datetime import UTC, date, datetime, timedelta

    from app.hosted.subscribe import plan_payload

    monthly, yearly = frozenset({MONTHLY}), frozenset({YEARLY_NEW, YEARLY})
    later = datetime.now(UTC) + timedelta(days=20)
    earlier = datetime.now(UTC) - timedelta(days=1)
    cases = [
        (plan_row(), (None, None, False, False, False)),
        (plan_row(state="paying", paid_by="paddle", paddle_status="active",
                  period_ends_at=later, paddle_price_id=YEARLY), ("yearly", later, True, False,
                                                                  False)),
        (plan_row(state="paying", paid_by="paddle", paddle_status="active", period_ends_at=later,
                  plan_ends_at=later, paddle_price_id=MONTHLY),
         ("monthly", later, True, True, False)),
        (plan_row(state="paying", paid_by="paddle", paddle_status="past_due",
                  period_ends_at=later, paddle_price_id=MONTHLY),
         ("monthly", later, True, False, True)),
        (plan_row(state="ended", why="plan", paddle_status="active", period_ends_at=later,
                  plan_ends_at=earlier, paddle_price_id=MONTHLY),
         (None, None, False, False, False)),
        (plan_row(state="paying", paid_by="gift", gift_until=date(9999, 12, 31),
                  paddle_status="active", period_ends_at=later, paddle_price_id=MONTHLY),
         ("gift", None, True, False, False)),
    ]
    for row, (kind, when, subscribed, cancel, retrying) in cases:
        seen = plan_payload(row, monthly, yearly)
        assert (seen["plan"], seen["subscribed"], seen["cancel_scheduled"],
                seen["payment_retrying"]) == (kind, subscribed, cancel, retrying), row
        assert seen["next_date"] == (None if when is None else when.isoformat())
        assert set(seen) == {"state", "why", "plan", "next_date", "subscribed",
                             "cancel_scheduled", "payment_retrying"}
    gift = plan_payload(plan_row(state="paying", paid_by="gift", gift_until=date(2027, 6, 30)),
                        monthly, yearly)
    assert gift["next_date"].startswith("2027-06-30T23:59:59")


def test_nothing_a_reader_is_told_names_a_number_a_countdown_or_unlimited() -> None:
    from app.hosted import subscribe

    lines = [subscribe._UNAVAILABLE[1], subscribe._TRY_AGAIN_SOON[1]]
    source = Path(subscribe.__file__).read_text(encoding="utf-8")
    lines += re.findall(r'_refused\(\d+, "[a-z_]+",\s*"([^"]+)"', source)
    lines += re.findall(r'"([^"]*’[^"]*)"', source)
    assert len(lines) >= 6
    for line in lines:
        assert not any(character.isdigit() for character in line), line
        for word in ("unlimited", "question", "left", "remaining", "$"):
            assert word not in line.casefold(), line


def test_the_regular_check_fetches_only_the_stored_ids_it_is_given_and_stops_when_paddle_is_down(
        monkeypatch) -> None:
    """The break it catches: the check fetching anything but the stored ids it is handed (one
    at a time, a pause between), applying a subscription whose answer is about another id or
    another product, or carrying on against a Paddle that is down."""
    from app.hosted import paddle_check

    stored = [(uuid.uuid4(), sub_id("one")), (uuid.uuid4(), sub_id("two")),
              (uuid.uuid4(), sub_id("three"))]
    monkeypatch.setattr(paddle_check, "plans_to_check", lambda dsn, size: stored[:size])
    applied: list = []
    monkeypatch.setattr(paddle_check, "apply_message",
                        lambda dsn, message: applied.append(message.subscription_id) or "applied")
    monkeypatch.setattr(paddle_check, "owes_cancel", lambda dsn, reader: False)

    def answer(request: httpx.Request) -> httpx.Response:
        sub = request.url.path.rsplit("/", 1)[1]
        if sub == sub_id("two"):
            return httpx.Response(200, json={"data": subscription_entity(sub_id("other"))})
        return httpx.Response(200, json={"data": subscription_entity(sub)})

    api, recorder = api_with(answer)
    pauses: list = []
    assert paddle_check.check("dsn", api, PRICES, size=3, pause=2.0,
                              sleep=pauses.append) == (3, 2, 1)
    assert [r.url.path for r in recorder.requests] == [f"/subscriptions/{s}" for _o, s in stored]
    assert all(r.method == "GET" for r in recorder.requests)
    assert applied == [sub_id("one"), sub_id("three")] and pauses == [2.0, 2.0]

    down, recorder = api_with(lambda r: httpx.Response(503))
    assert paddle_check.check("dsn", down, PRICES, size=3, pause=0,
                              sleep=lambda _s: None) == (0, 0, 1)
    assert len(recorder.requests) == 1


def test_the_regular_check_asks_for_an_owed_cancel_for_that_reader_alone(monkeypatch) -> None:
    """ADR 0045 step 7. The break it catches: the check not asking for the cancel owed to a
    reader who asked to delete their account, asking it for another reader, asking it before
    the reader's own subscription was fetched, or carrying on when Paddle is away."""
    from app.hosted import paddle_check

    owing, other = uuid.uuid4(), uuid.uuid4()
    stored = [(owing, sub_id("one")), (other, sub_id("two"))]
    monkeypatch.setattr(paddle_check, "plans_to_check", lambda dsn, size: stored[:size])
    monkeypatch.setattr(paddle_check, "apply_message", lambda dsn, message: "older")
    monkeypatch.setattr(paddle_check, "owes_cancel", lambda dsn, reader: reader == owing)
    asked: list = []
    outcomes = ["done"]
    monkeypatch.setattr(paddle_check, "cancel_owed",
                        lambda dsn, api, prices, reader: asked.append(reader) or outcomes[0])
    api, recorder = api_with(lambda r: httpx.Response(
        200, json={"data": subscription_entity(r.url.path.rsplit("/", 1)[1])}))
    pauses: list = []
    assert paddle_check.check("dsn", api, PRICES, size=5, pause=1.0,
                              sleep=pauses.append) == (2, 1, 0)
    assert asked == [owing] and pauses == [1.0, 1.0]
    assert [r.url.path for r in recorder.requests] == [f"/subscriptions/{s}" for _o, s in stored]

    for outcome, expected, fetched in (("unavailable", (1, 0, 1), 1), ("locked", (2, 0, 1), 2)):
        asked.clear()
        outcomes[0] = outcome
        api, recorder = api_with(lambda r: httpx.Response(
            200, json={"data": subscription_entity(r.url.path.rsplit("/", 1)[1])}))
        assert paddle_check.check("dsn", api, PRICES, size=5, pause=0,
                                  sleep=lambda _s: None) == expected
        assert asked == [owing] and len(recorder.requests) == fetched
