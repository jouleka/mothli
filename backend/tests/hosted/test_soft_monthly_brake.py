"""The soft brake on a paying reader's month (the owner's decision, 27 November 2026), without a
database.

Before: a paying reader was held to $1.00 of replies a UTC day and $4.00 a UTC month ($6.30 over
their first 30 paid days in place of the month), and at the month Mothli stopped until the 1st.
Now: past the month allowance their day drops to $0.25 and Mothli stays; only a ceiling of $8.00
a month, measured as the allowance is, stops them. The rule is ``_reader_cap_reached``; the spend
check's refusals around it are tested on PostgreSQL (``test_paddle_messages_postgres``).

The promise to readers who already pay: no guard is ever tighter than before. Proved here over a
grid of every boundary against the rule as it was, copied below unchanged.
"""

from __future__ import annotations

import dataclasses
import itertools
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.config import Settings
from app.hosted.companion import spend
from app.hosted.companion.spend import (
    SpendLimits,
    _reader_cap_reached,
    month_line,
    next_month,
    on_the_shelf,
    set_spend_limits,
    spend_limits,
)

D = Decimal
LIMITS = SpendLimits()
NO_OWN_CAPS = {"max_spend_usd_per_day": None, "max_spend_usd_per_month": None}


def before(used, policy, limits, *, notes=False, first_month=None):
    """The rule as it was before the brake (main at e93893e), unchanged."""
    day_cap = policy["max_spend_usd_per_day"]
    month_cap = policy["max_spend_usd_per_month"]
    if day_cap is None:
        day_cap = limits.notes_usd_per_day if notes else limits.reader_usd_per_day
    month_used, month = used["month"], "month"
    if month_cap is None and not notes:
        month_cap = limits.reader_usd_per_month
        if first_month is not None:
            month_cap, month_used, month = limits.first_paid_month_usd, first_month, "first_month"
    if used["day"] >= day_cap:
        return "day"
    if month_cap is not None and month_used >= month_cap:
        return month
    return None


def now(day, month, *, policy=NO_OWN_CAPS, limits=LIMITS, first=None, notes=False):
    return _reader_cap_reached({"day": D(day), "month": D(month)}, policy, limits, notes=notes,
                               first_month=None if first is None else D(first))


def test_the_defaults_are_the_owners_numbers() -> None:
    """The break it catches: a default other than the owner's (27 November 2026), in settings or
    in the spend check: $0.25 a day past the month, an $8.00 ceiling; the rest as before."""
    fields = Settings.model_fields
    assert fields["hosted_reader_usd_per_day_past_month"].default == 0.25
    assert fields["hosted_reader_usd_month_ceiling"].default == 8.00
    assert (LIMITS.reader_usd_per_day, LIMITS.reader_usd_per_month, LIMITS.first_paid_month_usd,
            LIMITS.reader_usd_per_day_past_month, LIMITS.reader_usd_month_ceiling) == (
        D("1.00"), D("4.00"), D("6.30"), D("0.25"), D("8.00"))


@pytest.mark.parametrize(("day", "month", "first", "was", "is_now"), [
    # Below the month allowance: unchanged.
    ("0", "3.99", None, None, None),
    ("0.99", "3.99", None, None, None),
    ("1.00", "3.99", None, "day", "day"),
    # Past the month allowance: refused before, now a quarter of a day.
    ("0", "4.00", None, "month", None),
    ("0.24", "4.00", None, "month", None),
    ("0.25", "4.00", None, "month", "day_past_month"),
    ("0.24", "7.99", None, "month", None),
    ("1.00", "7.99", None, "day", "day_past_month"),
    # At the ceiling: refused, as before.
    ("0", "8.00", None, "month", "month"),
    ("1.00", "8.00", None, "day", "month"),
    # The first paid month: its spend since the plan began, in place of the calendar month.
    ("0.99", "9", "6.29", None, None),
    ("1.00", "0", "6.29", "day", "day"),
    ("0.24", "0", "6.30", "first_month", None),
    ("0.25", "0", "6.30", "first_month", "day_past_month"),
    ("0", "0", "7.99", "first_month", None),
    ("0", "0", "8.00", "first_month", "first_month"),
])
def test_the_boundaries_before_and_now(day, month, first, was, is_now) -> None:
    """The break it catches: a reader below the month allowance treated differently from before,
    one past it stopped rather than given the smaller day, or one at the ceiling not stopped."""
    assert before({"day": D(day), "month": D(month)}, NO_OWN_CAPS, LIMITS,
                  first_month=None if first is None else D(first)) == was
    assert now(day, month, first=first) == is_now


def test_an_operators_own_caps_still_win() -> None:
    """The break it catches: the brake overriding an operator's ``owner_limits`` caps. A month
    cap of their own stops the reader as before (no brake); a day cap of their own is their day
    before and past the allowance; the notes account keeps its own caps."""
    own_month = {"max_spend_usd_per_day": None, "max_spend_usd_per_month": D("3")}
    assert now("0", "3", policy=own_month) == "month"
    assert now("0", "9", policy=own_month) == "month"
    assert now("1", "2", policy=own_month) == "day"
    own_day = {"max_spend_usd_per_day": D("2"), "max_spend_usd_per_month": None}
    assert now("1.50", "5", policy=own_day) is None
    assert now("2", "5", policy=own_day) == "day"
    assert now("0", "8", policy=own_day) == "month"
    assert now("10", "100", notes=True) == "day" and now("9", "100", notes=True) is None


def test_a_ceiling_set_below_the_allowance_is_the_allowance() -> None:
    """The break it catches: an operator's ceiling below the month (or the first month) making
    the rule tighter than the month alone was."""
    low = dataclasses.replace(LIMITS, reader_usd_month_ceiling=D("2"))
    assert now("0", "3.99", limits=low) is None
    assert now("0", "4", limits=low) == "month"
    assert now("0", "0", first="6.29", limits=low) is None
    assert now("0", "0", first="6.30", limits=low) == "first_month"


def test_never_tighter_than_before_for_any_reader_in_any_situation() -> None:
    """The owner's rule for readers who already pay. Over a grid of every boundary of every
    guard (just under, at and over each), with and without an operator's own caps, in and out of
    the first paid month, for the notes account too, and with a ceiling set below the allowance:
    wherever the rule before admitted a reply, it still does; below the month allowance (or with a
    month cap of the operator's own, or for the notes account) the answer is exactly as before."""
    amounts = [D(x) for x in ("0", "0.24", "0.25", "0.99", "1.00", "1.99", "2.00", "3.99",
                              "4.00", "6.29", "6.30", "7.99", "8.00", "9.99", "10.00", "20")]
    own_days = [None, D("0.10"), D("2")]
    own_months = [None, D("3"), D("50")]
    limits = [LIMITS, dataclasses.replace(LIMITS, reader_usd_month_ceiling=D("2")),
              dataclasses.replace(LIMITS, reader_usd_per_day_past_month=D("5"))]
    checked = 0
    for day, month, first, own_day, own_month, notes, caps in itertools.product(
            amounts, amounts, [None, *amounts], own_days, own_months, (False, True), limits):
        policy = {"max_spend_usd_per_day": own_day, "max_spend_usd_per_month": own_month}
        used = {"day": day, "month": month}
        was = before(used, policy, caps, notes=notes, first_month=first)
        is_now = _reader_cap_reached(used, policy, caps, notes=notes, first_month=first)
        if was is None:
            assert is_now is None, (used, policy, first, notes, caps)
        allowance = caps.first_paid_month_usd if first is not None else caps.reader_usd_per_month
        if notes or own_month is not None or (first if first is not None else month) < allowance:
            assert is_now == was, (used, policy, first, notes, caps)
        checked += 1
    assert checked > 100_000


def test_the_lines_name_when_and_nothing_else() -> None:
    """The break it catches: a line at the smaller day or the ceiling that names an amount, a
    count or a countdown, speaks of the machinery ("AI", "the chat", "model", "trial",
    "credits", "unlimited"), or does not say when Mothli is back; or the home chat told of one
    book."""
    day_line = spend._DAY_PAST_MONTH
    wall = month_line(date(2026, 12, 2))
    assert day_line == ("That’s a lot of good talk for one day! Let’s pick this up tomorrow. "
                        "Your book and your place are right here.")
    assert wall == ("We’ve talked our way through so much this month! I’ll be back on "
                    "2 December. Your book and your place are right here.")
    assert "tomorrow" in day_line
    for line in (day_line, wall):
        words = line.casefold().replace("’", " ").replace("!", " ").replace(".", " ").split()
        assert not {"ai", "chat", "model", "trial", "credits", "unlimited", "start"} & set(words)
        assert "$" not in line and "%" not in line
        assert on_the_shelf(line).endswith("Your books and your place in each are right here.")
    assert not any(c.isdigit() for c in day_line)
    assert [w for w in wall.split() if any(c.isdigit() for c in w)] == ["2"]
    assert not hasattr(spend, "_MONTH")              # the old wall line is gone


def test_the_day_named_at_the_ceiling_has_begun_everywhere_when_the_month_turns() -> None:
    """The break it catches: a day named at the ceiling on which a reader somewhere is still
    stopped (the month is counted in UTC; the day named has begun nowhere before it turns), or
    the turn of the year missed."""
    assert next_month(datetime(2026, 11, 27, 12, tzinfo=UTC)) == date(2026, 12, 2)
    assert next_month(datetime(2026, 11, 30, 23, 59, tzinfo=UTC)) == date(2026, 12, 2)
    assert next_month(datetime(2026, 12, 1, tzinfo=UTC)) == date(2027, 1, 2)
    assert next_month(datetime(2026, 12, 31, 23, tzinfo=UTC)) == date(2027, 1, 2)


def test_the_new_caps_are_checked_and_reach_the_spend_check() -> None:
    """The break it catches: a negative amount accepted by the process's caps, a setting
    without the others' bounds, or a cap set and not kept."""
    saved = spend_limits()
    try:
        set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=4, platform_usd_per_day=15,
                         reader_usd_per_day_past_month=D("0.3"), reader_usd_month_ceiling=9)
        assert (spend_limits().reader_usd_per_day_past_month,
                spend_limits().reader_usd_month_ceiling) == (D("0.3"), D("9"))
        for wrong in ({"reader_usd_per_day_past_month": -1},
                      {"reader_usd_month_ceiling": D("-0.01")}):
            with pytest.raises(ValueError):
                set_spend_limits(reader_usd_per_day=1, reader_usd_per_month=4,
                                 platform_usd_per_day=15, **wrong)
    finally:
        set_spend_limits(**dataclasses.asdict(saved))
    for name in ("hosted_reader_usd_per_day_past_month", "hosted_reader_usd_month_ceiling"):
        field = Settings.model_fields[name]
        assert any(getattr(item, "ge", None) == 0 for item in field.metadata), name
