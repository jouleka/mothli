"""The owner's gifts: his own account and test accounts as paying readers without Paddle (ADR 0045
section 8, step 2). Run in the operator container, as ``python -m app.hosted.limits`` is:

    python -m app.hosted.plans gift ACCOUNT_ID [--until YYYY-MM-DD]   # no date: for good
    python -m app.hosted.plans gift ACCOUNT_ID --end
    python -m app.hosted.plans show ACCOUNT_ID

A gift is written only through ``set_gift`` (migration 0038), which only the operator role may
execute; the web process has no path to it. ``show`` prints where the reader stands, the dates,
and their own chat spend since their free weeks began, from ``reader_plan_state``, and nothing
else: no book, no title, no words of any conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import date
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from app.config import Settings

# "For good": a far date, so the column stays a date.
FOR_GOOD = date(9999, 12, 31)


def set_gift(dsn: str, owner: uuid.UUID, until: date | None) -> bool:
    """Give ``owner`` a gift lasting to the end of ``until`` (a UTC day), or end it with None.
    False when no such account exists (nothing is written then)."""
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT public.set_gift(%s, %s::date)", (owner, until)).fetchone()[0]


def _free_weeks() -> tuple[int, Decimal]:
    """The free weeks' length and ceiling as the web process has them: ``HOSTED_FREE_DAYS`` and
    ``HOSTED_FREE_USD`` from this process's environment, or the app's defaults."""
    fields = Settings.model_fields
    days = os.environ.get("HOSTED_FREE_DAYS", fields["hosted_free_days"].default)
    usd = os.environ.get("HOSTED_FREE_USD", fields["hosted_free_usd"].default)
    return int(days), Decimal(str(usd))


def show_plan(dsn: str, owner: uuid.UUID) -> dict | None:
    """Where one reader stands, with the dates and their chat spend since the free weeks began;
    None for an unknown account."""
    free_days, free_usd = _free_weeks()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        if conn.execute("SELECT 1 FROM owner_limits WHERE owner_id=%s", (owner,)).fetchone() is None:
            return None
        row = conn.execute(
            "SELECT state, why, paid_by, gift_until, free_started_at, free_ends_at, free_spent_usd"
            " FROM public.reader_plan_state(%s, %s, %s)", (owner, free_days, free_usd)).fetchone()
    gift = row["gift_until"]
    return {
        "owner_id": str(owner),
        "state": row["state"],
        "why": row["why"],
        "paid_by": row["paid_by"],
        "gift_until": None if gift is None else "for good" if gift == FOR_GOOD else gift.isoformat(),
        "free_started_at": None if row["free_started_at"] is None
        else row["free_started_at"].isoformat(),
        "free_ends_at": None if row["free_ends_at"] is None else row["free_ends_at"].isoformat(),
        "chat_usd_since_free_start": str(row["free_spent_usd"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Give, end or show a reader's gift of Mothli")
    parser.add_argument("--dsn-env", default="DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)
    gift = sub.add_parser("gift", help="give a gift (for good unless --until), or --end it")
    gift.add_argument("owner", type=uuid.UUID)
    when = gift.add_mutually_exclusive_group()
    when.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                      help="the last day of the gift (UTC)")
    when.add_argument("--end", action="store_true", help="end the gift now")
    show = sub.add_parser("show", help="where the reader stands, the dates and their chat spend")
    show.add_argument("owner", type=uuid.UUID)
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    if args.command == "gift":
        until = None if args.end else args.until or FOR_GOOD
        if not set_gift(dsn, args.owner, until):
            parser.error("unknown account")
    shown = show_plan(dsn, args.owner)
    if shown is None:
        parser.error("unknown account")
    print(json.dumps(shown, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
