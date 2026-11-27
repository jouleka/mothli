"""The gate in front of Mothli (ADR 0045 step 5, sections 1, 6 and 9; ADR 0046).

Reading is free; all of Mothli (the chat in a book, the home chat, the cast, a person's card,
"Story so far", what Mothli has said) needs the plan, from the first question. Where a reader
stands is ``reader_plan_state``'s alone (``SpendStore.plan``): the routes, the spend check, the
session and the worker all read it, so they cannot disagree.

The gate only says yes or no. It runs before a gated route opens the reader's catalog, source,
search or story memory and before anything is reserved; it never changes what is read, searched
or sent. A reader who is not paying is answered ``402`` with ``{"detail": {"code":
"mothli_plan", "why": null | "plan"}}``, the shape of the app's other coded refusals: ``null``
for a new reader (never had a plan or a gift), ``plan`` for one whose plan stopped; the screens
choose the words by why. It depends on the reader's own row alone, never on a book or on another
reader.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.hosted.companion.spend import MOTHLI_PLAN, SpendStore
from app.hosted.tenant.models import OwnerId


def standing(dsn: str, owner_id: OwnerId) -> dict:
    """What the screens are told about the reader: ``state`` (``new``, ``paying`` or ``ended``)
    and ``why`` (``plan`` when ended, else None). No date, no amount, no count; the plan's dates
    and kind are ``GET /api/billing/plan``'s."""
    plan = SpendStore(dsn, owner_id).plan()
    return {"state": plan["state"], "why": plan["why"]}


def refusal(why: str | None) -> HTTPException:
    """The gate's answer to a reader who is not paying."""
    return HTTPException(402, {"code": MOTHLI_PLAN, "why": why})
