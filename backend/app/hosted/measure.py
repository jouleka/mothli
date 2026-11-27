"""What paying readers use and what the model calls cost, as numbers only, for the owner. Run in
the operator container, as the gift and limits commands are:

    python -m app.hosted.measure               # the last 30 UTC days
    python -m app.hosted.measure --days 7

It prints one JSON document, read with the operator's role (``DATABASE_URL``), which writes
nothing:

* ``readers``: one entry per paying reader (``reader_plan_state`` answers ``paying``), by account
  id: their replies per UTC day over the window (every day, zero included), their replies per UTC
  calendar month over all their history, and ``unique_uploads``, how many of their live books are
  files they uploaded that no other account holds.
* ``calls``: over every account's calls recorded in the window, per purpose and phase, the number
  of calls and the totals of full-price input, cache-written input, cache-read input and output
  tokens, and dollars. ``calls_without_split`` counts the calls among them whose cache split is
  not known (recorded before migration 0043, a failed call, a Jev check or an embedding); their
  whole input is counted as full price.

**A reply** is one ledger row of the reader's own conversation (not ``background``) whose purpose
is the writer's call of a reply: ``stream-reply`` (a reply in a book) or ``home-reply`` (a reply on
the home page), each with any suffix a settlement adds (``-failed-reserved``,
``-provider-limit-violation``). Each reply the reader asks for makes exactly one such writer call
(``app.stream_conversation.write_and_gate``); its sentence checks and second looks are recorded
under their own purposes and are not counted.

**A book nobody else had** is a reader's live book with ``source_kind`` ``upload`` whose two
fingerprints (``file_hash`` and ``split_hash``, ADR 0044 section 1) no live book of any other
account has, the notes account's editions and its own copies left aside. Its story memory (its
edition) is built for that reader alone. A book without both fingerprints has no edition to share
and is counted.

Nothing else is printed: no title, no author, no file name or fingerprint, no words of any
conversation, no email address.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

DEFAULT_DAYS = 30
# The writer's call of a reader's reply, settled as it is or under a settlement's suffix.
REPLY_PURPOSE = "^(stream|home)-reply(-|$)"

_PAYING = """SELECT plan.owner_id FROM public.reader_plans AS plan,
                    LATERAL public.reader_plan_state(plan.owner_id) AS state
             WHERE state.state='paying' ORDER BY plan.owner_id"""

_REPLIES_PER_DAY = """SELECT (recorded_at AT TIME ZONE 'UTC')::date AS day, count(*) AS replies
    FROM public.cost_ledger
    WHERE owner_id=%(o)s AND NOT background AND purpose ~ %(reply)s
      AND recorded_at >= %(since)s::date::timestamp AT TIME ZONE 'UTC'
    GROUP BY 1"""

_REPLIES_PER_MONTH = """SELECT to_char(recorded_at AT TIME ZONE 'UTC','YYYY-MM') AS month,
           count(*) AS replies
    FROM public.cost_ledger
    WHERE owner_id=%(o)s AND NOT background AND purpose ~ %(reply)s
    GROUP BY 1 ORDER BY 1"""

# The notes account is the one account that holds editions; its books (editions and any copy
# the operator's command left) are nobody's upload, so they never make a reader's book shared.
_UNIQUE_UPLOADS = """SELECT count(*) AS books FROM public.books AS mine
    WHERE mine.owner_id=%(o)s AND mine.deleted_at IS NULL AND mine.source_kind='upload'
      AND NOT EXISTS (
        SELECT 1 FROM public.books AS other
        WHERE other.file_hash=mine.file_hash AND other.split_hash=mine.split_hash
          AND other.owner_id<>mine.owner_id AND other.deleted_at IS NULL
          AND other.edition_of IS NULL
          AND NOT EXISTS (SELECT 1 FROM public.books AS edition
                          WHERE edition.owner_id=other.owner_id
                            AND edition.edition_of IS NOT NULL))"""

_CALLS = """SELECT purpose, phase, count(*) AS calls,
           COALESCE(sum(COALESCE(input_tokens,0) - COALESCE(cache_read_tokens,0)
                        - COALESCE(cache_write_tokens,0)),0)::bigint AS full_price_input_tokens,
           COALESCE(sum(cache_write_tokens),0)::bigint AS cache_write_input_tokens,
           COALESCE(sum(cache_read_tokens),0)::bigint AS cache_read_input_tokens,
           COALESCE(sum(output_tokens),0)::bigint AS output_tokens,
           COALESCE(sum(usd),0) AS usd,
           count(*) FILTER (WHERE cache_read_tokens IS NULL OR cache_write_tokens IS NULL)
             AS calls_without_split
    FROM public.cost_ledger
    WHERE recorded_at >= %(since)s::date::timestamp AT TIME ZONE 'UTC'
    GROUP BY purpose, phase
    ORDER BY purpose NULLS LAST, phase"""


def _today(conn) -> date:
    return conn.execute("SELECT (now() AT TIME ZONE 'UTC')::date AS today").fetchone()["today"]


def _usd(value: Decimal) -> str:
    """Dollars exactly as the ledger keeps them, without trailing zeros."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def measure(dsn: str, days: int = DEFAULT_DAYS) -> dict:
    """The numbers the module docstring describes, over the last ``days`` UTC days (today
    included)."""
    if days < 1:
        raise ValueError("days must be at least 1")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        today = _today(conn)
        since = today - timedelta(days=days - 1)
        window = [since + timedelta(days=offset) for offset in range(days)]
        readers = []
        for owner in [row["owner_id"] for row in conn.execute(_PAYING).fetchall()]:
            params = {"o": owner, "reply": REPLY_PURPOSE, "since": since}
            per_day = {row["day"]: row["replies"]
                       for row in conn.execute(_REPLIES_PER_DAY, params).fetchall()}
            per_month = {row["month"]: row["replies"]
                         for row in conn.execute(_REPLIES_PER_MONTH, params).fetchall()}
            unique = conn.execute(_UNIQUE_UPLOADS, params).fetchone()["books"]
            readers.append({
                "owner_id": str(owner),
                "replies_per_day": {day.isoformat(): per_day.get(day, 0) for day in window},
                "replies_per_month": per_month,
                "unique_uploads": unique,
            })
        calls = [{**row, "usd": _usd(row["usd"])}
                 for row in conn.execute(_CALLS, {"since": since}).fetchall()]
    return {"from": since.isoformat(), "to": today.isoformat(), "days": days,
            "reply_purpose": REPLY_PURPOSE, "readers": readers, "calls": calls}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paying readers' replies and unique uploads, and what the calls cost, as numbers")
    parser.add_argument("--dsn-env", default="DATABASE_URL")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"the window, in UTC days up to today (default {DEFAULT_DAYS})")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    print(json.dumps(measure(dsn, args.days), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
