"""The gate in front of Mothli (ADR 0045 step 5, sections 1, 6 and 9).

After their two free weeks a reader who does not pay keeps reading for free; all of Mothli (the
chat in a book, the home chat, the cast, a person's card, "Story so far", what Mothli has said)
needs the plan. Where a reader stands is ``reader_plan_state``'s alone (``SpendStore.plan``): the
routes, the spend check, the session and the worker all read it, so they cannot disagree.

The gate only says yes or no. It runs before a gated route opens the reader's catalog, source,
search or story memory and before anything is reserved; it never changes what is read, searched
or sent. An ended reader is answered ``402`` with ``{"detail": {"code": "mothli_plan", "why":
"time" | "ceiling" | "plan"}}``, the shape of the app's other coded refusals; the screens choose
the words by why. It depends on the reader's own row and chat alone, never on a book or on
another reader.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException

from app.hosted.companion.spend import MOTHLI_PLAN, SpendStore
from app.hosted.tenant.models import OwnerId

# The gentle note shows in the free weeks' last two days (ADR 0045 section 6).
ENDING_NOTE = timedelta(days=2)


def standing(dsn: str, owner_id: OwnerId) -> dict:
    """What the screens are told about the reader: ``state`` (``new``, ``free``, ``paying`` or
    ``ended``), ``why`` (``time``, ``ceiling`` or ``plan`` when ended, else None) and
    ``ending_note`` (the free weeks end within two days). No date, no amount, no count; the plan's
    dates and kind are ``GET /api/billing/plan``'s."""
    plan = SpendStore(dsn, owner_id).plan()
    ending = plan["state"] == "free" and plan["now"] >= plan["free_ends_at"] - ENDING_NOTE
    return {"state": plan["state"], "why": plan["why"], "ending_note": bool(ending)}


def refusal(why: str | None) -> HTTPException:
    """The gate's answer to an ended reader."""
    return HTTPException(402, {"code": MOTHLI_PLAN, "why": why})
