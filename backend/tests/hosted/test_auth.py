"""Hosted OIDC/session security contract (LIT-40)."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.hosted.auth.tokens import digest_token, new_token, pkce_challenge, safe_return_to


def test_hosted_mode_fails_closed_without_auth_configuration() -> None:
    with pytest.raises(ValidationError, match="HOSTED_AUTH_DSN"):
        Settings(_env_file=None, deployment_mode="hosted")


def test_hosted_mode_requires_https_oidc_urls() -> None:
    values = {
        "deployment_mode": "hosted",
        "hosted_auth_dsn": "postgresql://runtime@db/litlet",
        "hosted_tenant_dsn": "postgresql://tenant@db/litlet",
        "oidc_issuer": "http://issuer.example",
        "oidc_client_id": "litlet",
        "oidc_client_secret": "secret",
        "oidc_redirect_uri": "https://reader.example/api/auth/callback",
    }
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, **values)


def test_hosted_mode_rejects_a_wildcard_host_allowlist() -> None:
    values = {
        "deployment_mode": "hosted",
        "hosted_auth_dsn": "postgresql://runtime@db/litlet",
        "hosted_tenant_dsn": "postgresql://tenant@db/litlet",
        "oidc_issuer": "https://issuer.example",
        "oidc_client_id": "litlet",
        "oidc_client_secret": "not-a-real-secret",
        "oidc_redirect_uri": "https://reader.example/api/auth/callback",
        "trusted_hosts": "*",
    }
    with pytest.raises(ValidationError, match="TRUSTED_HOSTS"):
        Settings(_env_file=None, **values)


_HOSTED = {
    "deployment_mode": "hosted",
    "hosted_auth_dsn": "postgresql://runtime@db/litlet",
    "hosted_tenant_dsn": "postgresql://tenant@db/litlet",
    "oidc_issuer": "https://issuer.example",
    "oidc_client_id": "litlet",
    "oidc_client_secret": "not-a-real-secret",
    "oidc_redirect_uri": "https://reader.example/api/auth/callback",
}
DAY = 24 * 60 * 60


def test_a_reader_stays_signed_in_30_days_and_14_untouched_by_default() -> None:
    settings = Settings(_env_file=None, **_HOSTED)
    assert settings.session_absolute_ttl_seconds == 30 * DAY
    assert settings.session_idle_ttl_seconds == 14 * DAY


def test_idle_may_reach_but_not_exceed_the_absolute_life() -> None:
    assert Settings(
        _env_file=None, **_HOSTED,
        session_absolute_ttl_seconds=30 * DAY, session_idle_ttl_seconds=30 * DAY,
    ).session_idle_ttl_seconds == 30 * DAY
    with pytest.raises(ValidationError, match="may not exceed absolute"):
        Settings(
            _env_file=None, **_HOSTED,
            session_absolute_ttl_seconds=7 * DAY, session_idle_ttl_seconds=14 * DAY,
        )
    for field in ("session_absolute_ttl_seconds", "session_idle_ttl_seconds"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **_HOSTED, **{field: 30 * DAY + 1})


def test_pkce_uses_rfc7636_s256_and_tokens_are_high_entropy() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    token = new_token()
    assert len(token) == 43
    assert digest_token(token) == hashlib.sha256(token.encode("ascii")).digest()


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, "/"),
        ("/library?sort=recent", "/library?sort=recent"),
        ("https://evil.example/", "/"),
        ("//evil.example/path", "/"),
        ("/\\\\evil", "/"),
        ("/%5c%5cevil.example", "/"),
        ("/%2f%2fevil.example", "/"),
        ("/ok%0d%0aLocation:%20https://evil.example", "/"),
        ("login", "/"),
    ],
)
def test_return_path_cannot_be_an_open_redirect(candidate: str | None, expected: str) -> None:
    assert safe_return_to(candidate) == expected
