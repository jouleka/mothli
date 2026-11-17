"""Hosted mode: a reader deletes their account, and may change their mind (ADR 0041 part 8).

Asking to delete marks the account to be erased after 30 days, signs it out on every device and
stops its background work; from then on every reader route refuses it (``tenant/api.py``
``_session``). Signing in again within the 30 days gives only the choice to keep the account or
sign out. The erasure itself is the daily purge (``app/hosted/purge.py``), under its own role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.hosted.auth.api import (
    _NO_STORE,
    CSRF_COOKIE,
    SESSION_COOKIE,
    _clear_cookie,
    _now,
    authenticated_session,
    csrf_valid,
)
from app.hosted.tenant.api import _csrf_owner, _owner_input_guard
from app.hosted.tenant.models import OwnerId

router = APIRouter(prefix="/api/account", tags=["hosted-library"])


@router.post("/delete")
async def delete_account(request: Request, owner_id: OwnerId = Depends(_csrf_owner)):
    """Signed in, with the CSRF token, and only from an ordinary account (``_csrf_owner``
    refuses one already waiting to be erased)."""
    tenant = request.app.state.tenant_repository
    # Background work stops before the account is marked, so nothing it starts in between can
    # outlive the request, and again after, for a job added by a request that was already past
    # the sign-in check when this one began.
    await tenant.cancel_owner_jobs(owner_id)
    erase_after = await request.app.state.auth_repository.request_deletion(
        owner_id=owner_id.value, now=_now(request)
    )
    if erase_after is None:
        raise HTTPException(409, {"code": "account_not_active"})
    await tenant.cancel_owner_jobs(owner_id)
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
