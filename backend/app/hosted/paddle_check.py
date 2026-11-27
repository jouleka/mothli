"""The regular check of readers' plans against Paddle (ADR 0045 step 4).

Paddle's provisioning guide recommends a periodic reconciliation, so that a message lost, refused
or delivered out of order is repaired without waiting for the next renewal
(developer.paddle.com/build/subscriptions/provision-access-webhooks). The guide's example lists
subscriptions by customer; Paddle's account is shared with another product of the owner's, so this
lists nothing: it fetches, one at a time, the subscriptions Mothli itself stored
(``paddle_plans_to_check``: live, or changed in the last 30 days, a page per run, in a random
order) by their ids, and applies each through the same code as a message
(``billing.apply_message``: Mothli's prices only, newer than what was applied only). It pauses
between calls, stays far under Paddle's 240 requests a minute
(developer.paddle.com/api-reference/about/rate-limiting), and stops at the first sign that Paddle
is unavailable.

It is also how the cancel owed to a reader who asked to delete their account is guaranteed (ADR
0045 step 7): those plans come first in the list, and for each that Paddle's answer still shows
renewing, it asks for the cancel at the end of the period paid for through the code "Cancel my
plan" runs (``subscribe.change``), every run until Paddle's answer brings the scheduled end.
Likewise for a plan a refund or a chargeback ended (migration 0044): while Paddle's answer is
not yet cancelled, it asks for the immediate cancel (``subscribe.cancel_refunded``).

Run in the web container, which holds the key, the billing role's address and the prices, from
the server's crontab (the runbook, step 4c):

    docker compose -f deploy/compose.yaml exec -T web python -m app.hosted.paddle_check

It prints one line, ``checked N, changed N, failed N``, and nothing about any reader.
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg

from app.hosted.billing import Malformed, apply_message, read_subscription
from app.hosted.paddle_api import PaddleApi, PaddleRefused, PaddleUnavailable
from app.hosted.subscribe import cancel_owed, cancel_refunded, owes_cancel, refund_cancel_due


def plans_to_check(dsn: str, size: int) -> list[tuple]:
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        return conn.execute("SELECT reader, subscription FROM public.paddle_plans_to_check(%s)",
                            (size,)).fetchall()


def check(dsn: str, api: PaddleApi, prices: frozenset[str], *, size: int, pause: float,
          sleep=time.sleep) -> tuple[int, int, int]:
    """Fetch and apply up to ``size`` stored subscriptions; (checked, changed, failed)."""
    checked = changed = failed = 0
    for index, (reader, subscription) in enumerate(plans_to_check(dsn, size)):
        if index:
            sleep(pause)
        try:
            entity = api.subscription(subscription)
        except PaddleUnavailable:
            failed += 1
            break
        except PaddleRefused:
            failed += 1
            continue
        checked += 1
        if entity.get("id") != subscription:
            failed += 1
            continue
        try:
            message = read_subscription(entity, prices)
        except Malformed:
            failed += 1
            continue
        if message is not None and apply_message(dsn, message) == "applied":
            changed += 1
        # The cancel owed to a reader who asked to delete their account, until Paddle's answer
        # brings the scheduled end (ADR 0045 step 7).
        if owes_cancel(dsn, reader):
            sleep(pause)
            outcome = cancel_owed(dsn, api, prices, reader)
            if outcome == "unavailable":
                failed += 1
                break
            if outcome == "locked":
                failed += 1
            elif outcome == "done":
                changed += 1
        # The immediate cancel of a plan a refund or a chargeback ended, until Paddle's answer
        # says cancelled (migration 0044).
        if refund_cancel_due(dsn, reader) is not None:
            sleep(pause)
            outcome = cancel_refunded(dsn, api, prices, reader)
            if outcome == "unavailable":
                failed += 1
                break
            if outcome in ("locked", "refused"):
                failed += 1
            elif outcome == "done":
                changed += 1
    return checked, changed, failed


def main(argv: list[str] | None = None) -> int:
    from app.config import Settings

    parser = argparse.ArgumentParser(description="Check readers' Paddle plans against Paddle")
    parser.add_argument("--size", type=int, default=100,
                        help="at most this many plans in one run (default 100)")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds between two calls to Paddle (default 1)")
    args = parser.parse_args(argv)
    if not 1 <= args.size <= 1000 or not 0.25 <= args.pause <= 60:
        parser.error("--size is 1 to 1000 and --pause 0.25 to 60 seconds")
    settings = Settings()
    if settings.paddle_api_key is None or settings.hosted_billing_dsn is None:
        print("PADDLE_API_KEY and HOSTED_BILLING_DSN are required", file=sys.stderr)
        return 2
    api = PaddleApi(settings.paddle_api_key.get_secret_value(), settings.paddle_environment)
    try:
        checked, changed, failed = check(settings.hosted_billing_dsn.get_secret_value(), api,
                                         settings.paddle_prices(), size=args.size,
                                         pause=args.pause)
    finally:
        api.close()
    print(f"checked {checked}, changed {changed}, failed {failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
