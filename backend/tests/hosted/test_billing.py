"""Paddle's messages without a database (ADR 0045 step 3): the signature check, which messages
are Mothli's, and the settings. Messages are built and signed here; nothing calls Paddle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.hosted.billing import Malformed, read_message, verify_signature
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
