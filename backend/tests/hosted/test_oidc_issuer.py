"""The OIDC issuer is used exactly as the provider states it, with or without a final slash.

OpenID Connect Discovery 1.0 section 4: the discovery address drops one trailing slash
from the issuer, while the discovery document's issuer and the ID token's iss must
still equal the configured issuer character for character.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import ValidationError

from app.config import Settings
from app.hosted.auth.oidc import OIDCClient, OIDCError

HOST = "https://tenant.example.auth0.test"
CLIENT_ID = "mothli-client"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "https://reader.example/api/auth/callback"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
NONCE = "test-nonce"


def _settings(issuer: str) -> Settings:
    return Settings(
        _env_file=None,
        deployment_mode="hosted",
        hosted_auth_dsn="postgresql://runtime@db/litlet",
        hosted_tenant_dsn="postgresql://tenant@db/litlet",
        oidc_issuer=issuer,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri=REDIRECT_URI,
        hosted_credential_master_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        hosted_credential_key_version="test-v1",
    )


@pytest.mark.parametrize("issuer", [HOST, HOST + "/", HOST + "/tenant", HOST + "/tenant/"])
def test_settings_keep_the_issuer_exactly_as_the_provider_states_it(issuer: str) -> None:
    assert _settings(issuer).oidc_issuer == issuer


@pytest.mark.parametrize("issuer", [HOST + "//", HOST + "/tenant//"])
def test_settings_refuse_more_than_one_trailing_slash(issuer: str) -> None:
    with pytest.raises(ValidationError, match="at most one slash"):
        _settings(issuer)


@pytest.mark.parametrize(
    "issuer", [HOST + "/?tenant=1", HOST + "/#frag", "http://tenant.example/"]
)
def test_settings_keep_the_other_issuer_checks_with_a_trailing_slash(issuer: str) -> None:
    with pytest.raises(ValidationError):
        _settings(issuer)


class Provider:
    """A signed OIDC provider whose document and token issuers can differ on purpose."""

    def __init__(self, *, document_issuer: str, token_issuer: str) -> None:
        self.key = RSAKey.generate_key(auto_kid=True)
        self.document_issuer = document_issuer
        self.token_issuer = token_issuer
        self.requested: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url == HOST + "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.document_issuer,
                    "authorization_endpoint": HOST + "/authorize",
                    "token_endpoint": HOST + "/oauth/token",
                    "jwks_uri": HOST + "/.well-known/jwks.json",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_basic",
                        "client_secret_post",
                    ],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if url == HOST + "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [self.key.as_dict()]})
        if url == HOST + "/oauth/token":
            now = int(NOW.timestamp())
            claims = {
                "iss": self.token_issuer,
                "sub": "auth0|reader-1",
                "aud": CLIENT_ID,
                "exp": now + 300,
                "iat": now,
                "nonce": NONCE,
            }
            id_token = jwt.encode({"alg": "RS256", "kid": self.key.kid}, claims, self.key)
            return httpx.Response(200, json={"id_token": id_token, "token_type": "Bearer"})
        return httpx.Response(404)


def _sign_in(configured: str, provider: Provider):
    async def run():
        client = OIDCClient(
            issuer=configured,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scopes="openid email profile",
            signing_algorithms=("RS256",),
            clock_skew_seconds=60,
            timeout_seconds=5,
            transport=httpx.MockTransport(provider.handle),
        )
        try:
            return await client.exchange_and_validate(
                code="one-time-code", code_verifier="v" * 43, nonce=NONCE, now=NOW
            )
        finally:
            await client.close()

    return asyncio.run(run())


@pytest.mark.parametrize("configured", [HOST + "/", HOST])
def test_sign_in_succeeds_when_document_and_token_match_the_configured_issuer(
    configured: str,
) -> None:
    provider = Provider(document_issuer=configured, token_issuer=configured)
    claims = _sign_in(configured, provider)
    assert claims.issuer == configured
    assert claims.subject == "auth0|reader-1"
    assert provider.requested[0] == HOST + "/.well-known/openid-configuration"
    assert not any("//.well-known" in url for url in provider.requested)


@pytest.mark.parametrize(
    ("configured", "document_issuer", "token_issuer"),
    [
        # Configured with the slash: a document or token without it is refused.
        (HOST + "/", HOST, HOST + "/"),
        (HOST + "/", HOST + "/", HOST),
        # Configured without the slash: a document or token with it is refused.
        (HOST, HOST + "/", HOST),
        (HOST, HOST, HOST + "/"),
    ],
)
def test_issuer_comparisons_stay_exact_about_the_final_slash(
    configured: str, document_issuer: str, token_issuer: str
) -> None:
    provider = Provider(document_issuer=document_issuer, token_issuer=token_issuer)
    with pytest.raises(OIDCError):
        _sign_in(configured, provider)
