"""A return from the sign-in service that cannot become a session (2026-11-24): never a page of
JSON, always the front page with one fixed marker, the one-time cookie cleared, no session made,
and one log line naming the stage and nothing else. Stand-in repository and provider; the real
PostgreSQL and signed-token paths are in test_auth_postgres.py."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.hosted.auth.api import CSRF_COOKIE, OIDC_COOKIE, SESSION_COOKIE, SIGN_IN_AGAIN
from app.hosted.auth.models import IdentityClaims, LoginAttempt
from app.hosted.auth.oidc import OIDCError
from app.hosted.auth.repository import InactiveUserError
from app.main import create_app

ISSUER = "https://idp.example"
CODE = "a-provider-code-that-must-not-be-logged"
EMAIL = "reader-who-must-not-be-logged@example.test"
SUBJECT = "subject-that-must-not-be-logged"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class Repository:
    """Keeps attempts as the real one does: one use, bound to the browser, gone once expired."""

    def __init__(self) -> None:
        self.attempts: dict[tuple[bytes, bytes], tuple[LoginAttempt, datetime]] = {}
        self.inactive = False
        self.sessions = 0

    async def check_runtime_role(self) -> None:
        return None

    async def create_login_attempt(self, *, state_digest, browser_digest, issuer, code_verifier,
                                   nonce, return_to, now, expires_at) -> None:
        self.attempts[(state_digest, browser_digest)] = (
            LoginAttempt(issuer, code_verifier, nonce, return_to), expires_at)

    async def consume_login_attempt(self, *, state_digest, browser_digest, issuer, now):
        found = self.attempts.pop((state_digest, browser_digest), None)
        if found is None or found[0].issuer != issuer or found[1] <= now:
            return None
        return found[0]

    async def resolve_identity(self, claims, *, now) -> uuid.UUID:
        return uuid.uuid4()

    async def create_session(self, **_values) -> uuid.UUID:
        if self.inactive:
            raise InactiveUserError("account is not active")
        self.sessions += 1
        return uuid.uuid4()


class Provider:
    def __init__(self) -> None:
        self.refuse = False

    async def authorization_url(self, *, state, nonce, code_challenge) -> str:
        return f"{ISSUER}/authorize?state={state}"

    async def exchange_and_validate(self, *, code, code_verifier, nonce, now) -> IdentityClaims:
        if self.refuse:
            raise OIDCError("token exchange failed")
        return IdentityClaims(ISSUER, SUBJECT, "Wren", EMAIL, True)


@pytest.fixture()
def hosted():
    clock, repository, provider = Clock(), Repository(), Provider()
    settings = Settings(
        _env_file=None, deployment_mode="hosted",
        hosted_auth_dsn="postgresql://unused.invalid/mothli",
        hosted_tenant_dsn="postgresql://unused.invalid/mothli",
        oidc_issuer=ISSUER, oidc_client_id="mothli", oidc_client_secret="not-a-real-secret",
        oidc_redirect_uri="https://reader.example/api/auth/callback",
        trusted_hosts="reader.example",
    )
    app = create_app(
        settings, auth_repository=repository, tenant_repository=Repository(),
        oidc_client=provider, auth_clock=clock, object_storage=SimpleNamespace(close=lambda: None),
    )
    with TestClient(app, base_url="https://reader.example") as client:
        yield SimpleNamespace(client=client, clock=clock, repository=repository, provider=provider)


def _start(client: TestClient) -> str:
    login = client.get("/api/auth/login", follow_redirects=False)
    assert login.status_code == 302
    return parse_qs(urlsplit(login.headers["location"]).query)["state"][0]


def _assert_sent_back(response, caplog, stage: str, *secrets: str) -> None:
    assert response.status_code == 303
    assert response.headers["location"] == SIGN_IN_AGAIN == "/?signin=again"
    assert "no-store" in response.headers["cache-control"]
    cookies = response.headers.get_list("set-cookie")
    cleared = [item for item in cookies if item.startswith(f"{OIDC_COOKIE}=")]
    assert len(cleared) == 1 and "Max-Age=0" in cleared[0]
    assert not any(item.startswith((f"{SESSION_COOKIE}=", f"{CSRF_COOKIE}=")) for item in cookies)
    assert "Login could not be completed" not in response.text
    lines = [record.getMessage() for record in caplog.records if record.name == "app.hosted.auth.api"]
    assert lines == [f"sign-in return failed: {stage}"]
    for secret in (CODE, EMAIL, SUBJECT, "testclient", *secrets):
        assert secret not in caplog.text


def test_the_default_gives_a_reader_30_minutes_and_the_cookie_follows(hosted) -> None:
    login = hosted.client.get("/api/auth/login", follow_redirects=False)
    cookie = next(item for item in login.headers.get_list("set-cookie")
                  if item.startswith(f"{OIDC_COOKIE}="))
    assert "Max-Age=1800" in cookie


def test_back_after_more_than_30_minutes_lands_on_the_front_page(hosted, caplog) -> None:
    """Today's failure: the attempt expired while the reader was at the sign-in service."""
    caplog.set_level(logging.INFO)
    state = _start(hosted.client)
    cookie = hosted.client.cookies.get(OIDC_COOKIE)
    hosted.clock.value += timedelta(minutes=29)
    assert hosted.repository.attempts  # still good just inside the half hour
    hosted.clock.value += timedelta(minutes=1)
    response = hosted.client.get("/api/auth/callback", params={"state": state, "code": CODE},
                                 follow_redirects=False)
    _assert_sent_back(response, caplog, "attempt missing or expired", state, cookie)
    assert hosted.repository.sessions == 0


def test_back_within_30_minutes_signs_in(hosted) -> None:
    state = _start(hosted.client)
    hosted.clock.value += timedelta(minutes=25)
    response = hosted.client.get("/api/auth/callback", params={"state": state, "code": CODE},
                                 follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert hosted.repository.sessions == 1


@pytest.mark.parametrize("missing", ["state", "cookie"])
def test_no_state_or_cookie(hosted, caplog, missing: str) -> None:
    caplog.set_level(logging.INFO)
    state = _start(hosted.client)
    if missing == "cookie":
        hosted.client.cookies.clear()
    params = {"code": CODE} if missing == "state" else {"state": state, "code": CODE}
    response = hosted.client.get("/api/auth/callback", params=params, follow_redirects=False)
    _assert_sent_back(response, caplog, "no state or cookie", state)


def test_a_used_or_foreign_attempt(hosted, caplog) -> None:
    state = _start(hosted.client)
    cookie = hosted.client.cookies.get(OIDC_COOKIE)
    assert hosted.client.get("/api/auth/callback", params={"state": state, "code": CODE},
                             follow_redirects=False).status_code == 303
    # The same browser cookie again (as a replay would send it): the attempt was used once.
    hosted.client.cookies.set(OIDC_COOKIE, cookie, domain="reader.example", path="/")
    caplog.set_level(logging.INFO)
    replay = hosted.client.get("/api/auth/callback", params={"state": state, "code": CODE},
                               follow_redirects=False)
    _assert_sent_back(replay, caplog, "attempt missing or expired", state)
    assert hosted.repository.sessions == 1


@pytest.mark.parametrize(
    ("sent", "logged"),
    [
        ("access_denied", "access_denied"),
        ("login_required", "login_required"),
        ("temporarily_unavailable", "temporarily_unavailable"),
        ("something <script>made-up</script>", "other"),
    ],
)
def test_the_provider_said_error(hosted, caplog, sent: str, logged: str) -> None:
    caplog.set_level(logging.INFO)
    state = _start(hosted.client)
    response = hosted.client.get("/api/auth/callback", params={"state": state, "error": sent},
                                 follow_redirects=False)
    _assert_sent_back(response, caplog, f"provider said error {logged}", state)
    if logged == "other":
        assert "made-up" not in caplog.text and "made-up" not in response.text
    # One use only: the attempt is gone even though the provider said no.
    assert not hosted.repository.attempts


def test_no_code(hosted, caplog) -> None:
    caplog.set_level(logging.INFO)
    state = _start(hosted.client)
    response = hosted.client.get("/api/auth/callback", params={"state": state},
                                 follow_redirects=False)
    _assert_sent_back(response, caplog, "no code", state)


def test_the_exchange_or_validation_failed(hosted, caplog) -> None:
    caplog.set_level(logging.INFO)
    hosted.provider.refuse = True
    state = _start(hosted.client)
    response = hosted.client.get("/api/auth/callback", params={"state": state, "code": CODE},
                                 follow_redirects=False)
    _assert_sent_back(response, caplog, "exchange or validation failed", state)
    assert hosted.repository.sessions == 0


def test_an_inactive_account(hosted, caplog) -> None:
    caplog.set_level(logging.INFO)
    hosted.repository.inactive = True
    state = _start(hosted.client)
    response = hosted.client.get("/api/auth/callback", params={"state": state, "code": CODE},
                                 follow_redirects=False)
    _assert_sent_back(response, caplog, "account inactive", state)


def test_the_marker_never_takes_anything_from_the_request(hosted, caplog) -> None:
    caplog.set_level(logging.INFO)
    response = hosted.client.get(
        "/api/auth/callback",
        params={"state": "https://evil.example", "error": "x", "return_to": "//evil.example"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?signin=again"
    assert "evil" not in caplog.text
