"""Shared hosted limit outcomes and content-free operator policy tooling (LIT-47)."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row


class LimitExceededError(RuntimeError):
    def __init__(
        self,
        code: str,
        limit: int | str,
        retry_after_seconds: int | None,
        action: str,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds
        self.action = action


# The dollar caps: lifetime (LIT-47), and per UTC day and month for the companion the platform
# pays for (ADR 0041 part 3; NULL means the HOSTED_READER_USD_PER_DAY / _PER_MONTH default).
_SPEND_CAPS = ("max_spend_usd", "max_spend_usd_per_day", "max_spend_usd_per_month")


def _policy_row(row: dict) -> dict:
    value = dict(row)
    value["owner_id"] = str(value["owner_id"])
    for money in _SPEND_CAPS:
        if value.get(money) is not None:
            value[money] = str(value[money])
    if value.get("spend_usd") is not None:
        value["spend_usd"] = str(value["spend_usd"])
    if value.get("reserved_usd") is not None:
        value["reserved_usd"] = str(value["reserved_usd"])
    if hasattr(value.get("updated_at"), "isoformat"):
        value["updated_at"] = value["updated_at"].isoformat()
    return value


def inspect_limits(dsn: str, owner_id: uuid.UUID | None = None) -> list[dict]:
    """Return opaque owner IDs, numeric policy, and aggregate usage only."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT limits.*,
                   (SELECT count(*) FROM books AS book
                    WHERE book.owner_id=limits.owner_id AND book.deleted_at IS NULL) AS books,
                   COALESCE((SELECT sum(object.byte_size) FROM source_objects AS object
                    WHERE object.owner_id=limits.owner_id AND object.deleted_at IS NULL),0)
                     AS library_bytes,
                   (SELECT count(*) FROM jobs AS job WHERE job.owner_id=limits.owner_id
                    AND job.state IN ('waiting_configuration','pending','leased','running'))
                     AS active_jobs,
                   COALESCE((SELECT sum(entry.usd) FROM cost_ledger AS entry
                    WHERE entry.owner_id=limits.owner_id),0) AS spend_usd,
                   COALESCE((SELECT sum(reservation.reserved_usd) FROM cost_reservations AS reservation
                    WHERE reservation.owner_id=limits.owner_id AND reservation.state='reserved'),0)
                     AS reserved_usd
            FROM owner_limits AS limits
            WHERE (%s::uuid IS NULL OR limits.owner_id=%s::uuid)
            ORDER BY limits.owner_id
            """,
            (owner_id, owner_id),
        ).fetchall()
    return [_policy_row(row) for row in rows]


def update_limits(dsn: str, owner_id: uuid.UUID, updates: dict[str, object]) -> dict | None:
    allowed = {
        "max_upload_bytes",
        "max_library_bytes",
        "max_books",
        "max_active_jobs",
        "requests_per_window",
        "request_window_seconds",
        "max_provider_concurrency",
        "max_spend_usd",
        "max_spend_usd_per_day",
        "max_spend_usd_per_month",
        "max_companion_calls_in_flight",
    }
    if not updates or not set(updates) <= allowed:
        raise ValueError("one or more reviewed limit fields are required")
    assignments = ",".join(f"{name}=%({name})s" for name in sorted(updates))
    values = {**updates, "owner_id": owner_id}
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))", (owner_id,)
        )
        row = conn.execute(
            f"UPDATE owner_limits SET {assignments},updated_at=now() "  # noqa: S608
            "WHERE owner_id=%(owner_id)s RETURNING *",
            values,
        ).fetchone()
    return None if row is None else _policy_row(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or adjust content-free hosted owner limits")
    parser.add_argument("--dsn-env", default="DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show")
    show.add_argument("--owner", type=uuid.UUID)
    set_parser = sub.add_parser("set")
    set_parser.add_argument("owner", type=uuid.UUID)
    for field in (
        "max_upload_bytes",
        "max_library_bytes",
        "max_books",
        "max_active_jobs",
        "requests_per_window",
        "request_window_seconds",
        "max_provider_concurrency",
        "max_companion_calls_in_flight",
    ):
        set_parser.add_argument("--" + field.replace("_", "-"), dest=field, type=int)
    set_parser.add_argument("--max-spend-usd", type=Decimal)
    set_parser.add_argument("--clear-spend-limit", action="store_true")
    set_parser.add_argument("--max-spend-usd-per-day", type=Decimal)
    set_parser.add_argument("--clear-day-spend-limit", action="store_true",
                            help="use HOSTED_READER_USD_PER_DAY for this owner")
    set_parser.add_argument("--max-spend-usd-per-month", type=Decimal)
    set_parser.add_argument("--clear-month-spend-limit", action="store_true",
                            help="use HOSTED_READER_USD_PER_MONTH for this owner")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f"environment variable {args.dsn_env!r} is not set")
    if args.command == "show":
        print(json.dumps(inspect_limits(dsn, args.owner), sort_keys=True))
        return 0
    fields = (
        "max_upload_bytes",
        "max_library_bytes",
        "max_books",
        "max_active_jobs",
        "requests_per_window",
        "request_window_seconds",
        "max_provider_concurrency",
        "max_companion_calls_in_flight",
    )
    updates = {field: getattr(args, field) for field in fields if getattr(args, field) is not None}
    for cap, clear, flag in (
        ("max_spend_usd", args.clear_spend_limit, "--clear-spend-limit"),
        ("max_spend_usd_per_day", args.clear_day_spend_limit, "--clear-day-spend-limit"),
        ("max_spend_usd_per_month", args.clear_month_spend_limit, "--clear-month-spend-limit"),
    ):
        if getattr(args, cap) is not None:
            updates[cap] = getattr(args, cap)
        if clear:
            if cap in updates:
                parser.error(f"choose either --{cap.replace('_', '-')} or {flag}")
            updates[cap] = None
    try:
        result = update_limits(dsn, args.owner, updates)
    except (ValueError, psycopg.errors.CheckViolation) as exc:
        parser.error(str(exc))
    if result is None:
        parser.error("unknown owner")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
