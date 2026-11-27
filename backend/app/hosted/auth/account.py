"""Hosted mode: a reader deletes their account, and may change their mind (ADR 0041 part 8).

Asking to delete marks the account to be erased after 30 days, signs it out on every device and
stops its background work; from then on every reader route refuses it (``tenant/api.py``
``_session``). Signing in again within the 30 days gives only the choice to keep the account or
sign out. The erasure itself is the daily purge (``app/hosted/purge.py``), under its own role.

A reader paying through Paddle is never charged again once they ask (ADR 0045 step 7): see
``delete_account``. Coming back within the 30 days keeps the account and leaves the plan ending
at the period's end; "Keep my plan" (``POST /api/billing/keep``) is what renews it again.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.hosted import subscribe
from app.hosted.auth.api import (
    _NO_STORE,
    CSRF_COOKIE,
    SESSION_COOKIE,
    _clear_cookie,
    _now,
    authenticated_session,
    csrf_valid,
)
from app.hosted.companion import CompanionDatabaseBusy
from app.hosted.limits import LimitExceededError
from app.hosted.tenant.api import _csrf_owner, _limit_failure, _owner_input_guard
from app.hosted.tenant.export import ExportGate, ExportRefused, make_copy, stream
from app.hosted.tenant.models import OwnerId

router = APIRouter(prefix="/api/account", tags=["hosted-library"])


def _refused(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status,
                        headers=_NO_STORE)


@router.get("/export")
async def export_account(request: Request, _guard: None = Depends(_owner_input_guard)):
    """The reader's copy of their data, one zip (``app/hosted/tenant/export.py``). Read-only.
    Signed in, and the one more thing, with keeping the account and signing out, that a reader
    waiting to be erased may do: so it checks the session itself instead of ``_owner``, and still
    counts against the reader's request limit."""
    authenticated = await authenticated_session(request)
    if authenticated is None:
        raise HTTPException(401, "Authentication required")
    principal = authenticated.principal
    owner_id = OwnerId(principal.owner_id)
    tenant = request.app.state.tenant_repository
    try:
        await tenant.consume_request_limit(owner_id)
    except LimitExceededError as exc:
        raise _limit_failure(exc) from exc
    gate = getattr(request.app.state, "account_export_gate", None)
    if gate is None:
        gate = request.app.state.account_export_gate = ExportGate()
    try:
        gate.admit(owner_id)
    except ExportRefused as refused:
        return _refused(refused.status, refused.code, refused.message)
    made = False
    try:
        joined = await request.app.state.auth_repository.account_joined(owner_id=owner_id.value)
        who = {"name": principal.display_name, "email": principal.email,
               "joined": None if joined is None else joined.isoformat()}
        try:
            copy = await make_copy(request=request, owner_id=owner_id, who=who)
        except CompanionDatabaseBusy:
            return _refused(503, "export_busy",
                            "Mothli is busy right now. Try again in a minute.")
        try:
            await tenant.record_account_export(owner_id)
        except BaseException:
            copy.close()
            raise
        made = True
    finally:
        if not made:
            gate.finished(owner_id, made=False)
    day = _now(request).strftime("%Y-%m-%d")
    return StreamingResponse(
        stream(copy, lambda: gate.finished(owner_id, made=True)),
        media_type="application/zip",
        headers={**_NO_STORE,
                 "Content-Disposition": f'attachment; filename="mothli-my-data-{day}.zip"'})


def _billing(request: Request) -> str | None:
    """The billing role's address where this server manages Paddle plans, else None."""
    settings = request.app.state.settings
    if settings.paddle_api_key is None or settings.hosted_billing_dsn is None:
        return None
    return settings.hosted_billing_dsn.get_secret_value()


@router.post("/delete")
async def delete_account(request: Request, owner_id: OwnerId = Depends(_csrf_owner)):
    """Signed in, with the CSRF token, and only from an ordinary account (``_csrf_owner``
    refuses one already waiting to be erased).

    A paying reader is never charged again (ADR 0045 step 7): before the account is marked, the
    cancel of their plan at the end of the period paid for is recorded as owed to them, and once
    it is marked that cancel is asked of Paddle through the code "Cancel my plan" runs. Paddle
    unreachable, slow or refusing changes nothing for the request: the owed mark stays and the
    regular check asks again every hour until Paddle's answer brings the scheduled end. A reader
    with no live plan (a gift only, new, ended, or already cancelled) costs no call."""
    tenant = request.app.state.tenant_repository
    billing = _billing(request)
    if billing is not None:
        try:
            await run_in_threadpool(subscribe.owe_cancel, billing, owner_id.value)
        except psycopg.Error:
            # Nothing has happened yet: the reader is told to try again.
            raise HTTPException(503, {"code": "account_busy"}) from None
    # Background work stops before the account is marked, so nothing it starts in between can
    # outlive the request, and again after, for a job added by a request that was already past
    # the sign-in check when this one began.
    await tenant.cancel_owner_jobs(owner_id)
    erase_after = await request.app.state.auth_repository.request_deletion(
        owner_id=owner_id.value, now=_now(request)
    )
    if erase_after is None:
        # Asked to be deleted by another request meanwhile: its owed cancel stands.
        raise HTTPException(409, {"code": "account_not_active"})
    await tenant.cancel_owner_jobs(owner_id)
    if billing is not None:
        await run_in_threadpool(subscribe.cancel_owed, billing, subscribe.paddle_api(request),
                                request.app.state.settings.paddle_prices(), owner_id.value)
    response = JSONResponse({"erase_after": erase_after.isoformat()}, headers=_NO_STORE)
    # Every session of this reader has ended, this one included.
    _clear_cookie(response, SESSION_COOKIE, http_only=True)
    _clear_cookie(response, CSRF_COOKIE, http_only=False)
    return response


@router.post("/keep", status_code=204)
async def keep_account(request: Request, _guard: None = Depends(_owner_input_guard)):
    """The one thing, with signing out, that a reader waiting to be erased may do. An ordinary
    account has nothing to keep and is answered the same."""
    authenticated = await authenticated_session(request)
    if authenticated is None:
        raise HTTPException(401, "Authentication required")
    if not csrf_valid(request, authenticated):
        raise HTTPException(403, "CSRF validation failed")
    if authenticated.principal.erase_after is not None:
        restored = await request.app.state.auth_repository.restore_account(
            owner_id=authenticated.principal.owner_id
        )
        if not restored:
            # The erasure began between the sign-in check and now.
            raise HTTPException(409, {"code": "account_not_pending"})
    return Response(status_code=204, headers=_NO_STORE)
