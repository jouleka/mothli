"""Hosted browser authentication endpoints.

Only opaque random values cross the cookie boundary. Authorization attempts, PKCE verifiers,
identity mappings, sessions, expiry, and revocation remain server-side.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.hosted.auth.models import Principal
from app.hosted.auth.oidc import OIDCError
from app.hosted.auth.repository import InactiveUserError
from app.hosted.auth.tokens import digest_token, new_token, pkce_challenge, safe_return_to

SESSION_COOKIE = "__Host-litlet-session"
CSRF_COOKIE = "__Host-litlet-csrf"
OIDC_COOKIE = "__Host-litlet-oidc"
CSRF_HEADER = "X-CSRF-Token"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# Where a return from the sign-in service that cannot become a session lands: the front page, with
# one fixed marker the app turns into a kind line and a sign-in button. A literal, never built from
# the request, so nothing is reflected and nothing can point elsewhere; it never says which step
# failed. The server's log does, in one content-free line (_RETURN_FAILED).
SIGN_IN_AGAIN = "/?signin=again"
_LOG = logging.getLogger(__name__)
# The standard OAuth error codes a provider may send back; anything else is logged as "other".
_PROVIDER_ERRORS = (
    "access_denied", "login_required", "consent_required", "interaction_required",
    "server_error", "temporarily_unavailable",
)
# One static line per failed return (ADR 0029: only reviewed static messages are logged; this
# table is checked by the same gate). No state, code, cookie, email, subject or address.
_RETURN_FAILED = {
    "no state or cookie": "sign-in return failed: no state or cookie",
    "attempt missing or expired": "sign-in return failed: attempt missing or expired",
    "provider said access_denied": "sign-in return failed: provider said error access_denied",
    "provider said login_required": "sign-in return failed: provider said error login_required",
    "provider said consent_required":
        "sign-in return failed: provider said error consent_required",
    "provider said interaction_required":
        "sign-in return failed: provider said error interaction_required",
    "provider said server_error": "sign-in return failed: provider said error server_error",
    "provider said temporarily_unavailable":
        "sign-in return failed: provider said error temporarily_unavailable",
    "provider said other": "sign-in return failed: provider said error other",
    "no code": "sign-in return failed: no code",
    "exchange or validation failed": "sign-in return failed: exchange or validation failed",
    "account inactive": "sign-in return failed: account inactive",
}

router = APIRouter(prefix="/api/auth", tags=["authentication"])


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    principal: Principal
    session_digest: bytes


def _now(request: Request) -> datetime:
    value = request.app.state.auth_clock()
    if value.tzinfo is None:
        raise RuntimeError("authentication clock must return an aware datetime")
    return value.astimezone(UTC)


def _digest_cookie(value: str | None) -> bytes | None:
    if value is None or len(value) != 43:
        return None
    try:
        return digest_token(value)
    except (UnicodeEncodeError, ValueError):
        return None


def _set_cookie(response: Response, name: str, value: str, *, http_only: bool, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=http_only,
        samesite="lax",
    )


def _clear_cookie(response: Response, name: str, *, http_only: bool) -> None:
    response.delete_cookie(
        name,
        path="/",
        secure=True,
        httponly=http_only,
        samesite="lax",
    )


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code, headers=_NO_STORE)


def _sign_in_again(stage: str) -> RedirectResponse:
    """A return that cannot become a session: one log line naming the stage, then the front page
    with the fixed marker, the one-time sign-in cookie cleared and no session made."""
    _LOG.warning(_RETURN_FAILED[stage])
    response = RedirectResponse(SIGN_IN_AGAIN, status_code=303, headers=_NO_STORE)
    _clear_cookie(response, OIDC_COOKIE, http_only=True)
    return response


@router.get("/login")
async def login(request: Request, return_to: str | None = None):
    settings = request.app.state.settings
    repository = request.app.state.auth_repository
    oidc = request.app.state.oidc_client
    now = _now(request)
    state = new_token()
    browser = new_token()
    verifier = new_token()
    nonce = new_token()
    try:
        authorization_url = await oidc.authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=pkce_challenge(verifier),
        )
        await repository.create_login_attempt(
            state_digest=digest_token(state),
            browser_digest=digest_token(browser),
            issuer=settings.oidc_issuer,
            code_verifier=verifier,
            nonce=nonce,
            return_to=safe_return_to(return_to),
            now=now,
            expires_at=now + timedelta(seconds=settings.oidc_attempt_ttl_seconds),
        )
    except OIDCError:
        return _error(503, "Login is temporarily unavailable")

    response = RedirectResponse(authorization_url, status_code=302, headers=_NO_STORE)
    _set_cookie(
        response,
        OIDC_COOKIE,
        browser,
        http_only=True,
        max_age=settings.oidc_attempt_ttl_seconds,
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
):
    settings = request.app.state.settings
    repository = request.app.state.auth_repository
    oidc = request.app.state.oidc_client
    state_digest = _digest_cookie(state)
    browser_digest = _digest_cookie(request.cookies.get(OIDC_COOKIE))
    # Always a browser's top-level navigation back from the sign-in service: a failure lands on
    # the front page, which says so kindly (SIGN_IN_AGAIN), never on a page of JSON.
    if state_digest is None or browser_digest is None:
        return _sign_in_again("no state or cookie")
    now = _now(request)
    attempt = await repository.consume_login_attempt(
        state_digest=state_digest,
        browser_digest=browser_digest,
        issuer=settings.oidc_issuer,
        now=now,
    )
    if attempt is None:
        return _sign_in_again("attempt missing or expired")
    if error is not None:
        known = error if error in _PROVIDER_ERRORS else "other"
        return _sign_in_again(f"provider said {known}")
    if code is None:
        return _sign_in_again("no code")

    try:
        claims = await oidc.exchange_and_validate(
            code=code,
            code_verifier=attempt.code_verifier,
            nonce=attempt.nonce,
            now=now,
        )
        owner_id = await repository.resolve_identity(claims, now=now)
        session_token = new_token()
        csrf_token = new_token()
        old_session_digest = _digest_cookie(request.cookies.get(SESSION_COOKIE))
        await repository.create_session(
            owner_id=owner_id,
            session_digest=digest_token(session_token),
            csrf_digest=digest_token(csrf_token),
            issuer=claims.issuer,
            old_session_digest=old_session_digest,
            now=now,
            expires_at=now + timedelta(seconds=settings.session_absolute_ttl_seconds),
        )
    except OIDCError:
        return _sign_in_again("exchange or validation failed")
    except InactiveUserError:
        return _sign_in_again("account inactive")

    response = RedirectResponse(attempt.return_to, status_code=303, headers=_NO_STORE)
    _clear_cookie(response, OIDC_COOKIE, http_only=True)
    _set_cookie(
        response,
        SESSION_COOKIE,
        session_token,
        http_only=True,
        max_age=settings.session_absolute_ttl_seconds,
    )
    _set_cookie(
        response,
        CSRF_COOKIE,
        csrf_token,
        http_only=False,
        max_age=settings.session_absolute_ttl_seconds,
    )
    return response


async def authenticated_session(request: Request) -> AuthenticatedSession | None:
    digest = _digest_cookie(request.cookies.get(SESSION_COOKIE))
    if digest is None:
        return None
    principal = await request.app.state.auth_repository.authenticate_session(
        session_digest=digest,
        now=_now(request),
        idle_ttl=timedelta(seconds=request.app.state.settings.session_idle_ttl_seconds),
    )
    if principal is None:
        return None
    return AuthenticatedSession(principal=principal, session_digest=digest)


def csrf_valid(request: Request, authenticated: AuthenticatedSession) -> bool:
    """Validate the session-bound double-submit token for any hosted unsafe request."""
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    header_digest = _digest_cookie(header)
    return bool(
        header is not None
        and cookie is not None
        and len(header) == 43
        and len(cookie) == 43
        and secrets.compare_digest(header, cookie)
        and header_digest is not None
        and secrets.compare_digest(header_digest, authenticated.principal.csrf_digest)
    )


PENDING_DELETION = "account_pending_deletion"


def pending_deletion_detail(erase_after: datetime) -> dict[str, str]:
    """What every reader route answers while the account waits to be erased: a code the app
    tells apart from any other refusal, and the moment, so it can name the day."""
    return {"code": PENDING_DELETION, "erase_after": erase_after.astimezone(UTC).isoformat()}


def _mothli(request: Request, owner) -> dict | None:
    """Where the reader stands with Mothli (ADR 0045 step 5, ``gate.standing``): ``state``,
    ``why`` and ``ending_note``, or None where this server has no chat or the plan cannot be read
    just now (the gate still answers every Mothli route itself)."""
    if not request.app.state.settings.companion_chat():
        return None
    from app.hosted.companion import CompanionDatabaseBusy, gate
    from app.hosted.tenant.models import OwnerId

    try:
        return gate.standing(request.app.state.companion_dsn, OwnerId(owner))
    except (CompanionDatabaseBusy, psycopg.Error):
        return None


@router.get("/session")
async def session(request: Request):
    authenticated = await authenticated_session(request)
    if authenticated is None:
        return _error(401, "Authentication required")
    principal = authenticated.principal
    payload = {
        "user": {
            "id": str(principal.owner_id),
            "display_name": principal.display_name,
            "email": principal.email,
        },
        "expires_at": principal.expires_at.isoformat(),
        # Whether this server has Mothli's chat: the app shows no way into one where it has not.
        "chat": request.app.state.settings.companion_chat(),
        # Where the reader stands with Mothli; the plan's kind and dates are /api/billing/plan's.
        "mothli": await run_in_threadpool(_mothli, request, principal.owner_id),
    }
    if principal.erase_after is not None:
        payload["erase_after"] = principal.erase_after.astimezone(UTC).isoformat()
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/logout")
async def logout(request: Request):
    authenticated = await authenticated_session(request)
    if authenticated is None:
        return _error(401, "Authentication required")
    if not csrf_valid(request, authenticated):
        return _error(403, "CSRF validation failed")
    await request.app.state.auth_repository.revoke_session(
        session_digest=authenticated.session_digest,
        now=_now(request),
    )
    response = Response(status_code=204, headers=_NO_STORE)
    _clear_cookie(response, SESSION_COOKIE, http_only=True)
    _clear_cookie(response, CSRF_COOKIE, http_only=False)
    return response
