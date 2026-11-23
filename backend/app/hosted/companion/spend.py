"""What the companion spends, for one hosted reader, against the reader's and the platform's caps.

ADR 0041 part 3, section 6. Same names, arguments and return shapes as the local catalog's cost
methods (``app.catalog.catalog.Catalog``), which ``budgeted_completion`` and ``budgeted_jev`` call
around every model call: reserve before, note the actual and settle after, or release a call the
provider refused outright. Nothing here decides when a call is charged; those two wrappers do,
unchanged, in both modes.

The owner of Mothli pays. ``reserve_cost`` checks, in one transaction under the owner's lock
(the lock the hosted repository already takes for spend): the reader's UTC day, the reader's UTC
month, the reader's lifetime cap if one is set, the reader's companion calls in flight, and then
the platform's UTC day (its row locked). A refusal is raised before any money is spent, as a
``RuntimeError``, so the two wrappers turn it into ``CostCeilingExceeded`` unchanged.

The reader's day and month count what was really spent: the ledger, where the wrappers also put a
call whose outcome is unknown (a timeout) at its reserved size. A call is refused once that spend
has reached the cap, not when its own worst case would pass it, so a reader is never turned away
with allowance left. A reply a reader is waiting on is admitted once, by its first call; its later
calls (the sentence checks and second looks) are then never refused for a dollar cap, so a reply
that began under the cap is finished and not cut off mid-way (``ReplyCatalog`` in ``store.py``).
A reader can therefore end a day past the cap by at most the replies under way when it was
reached.

A reader's allowance counts only their conversation (ADR 0041 section 6, amended 20 November
2026). Mothli's background work for them, their chapter notes and recollections, is paid through
a store marked ``_BACKGROUND`` (the worker's catalog): its rows carry ``background`` (migration
0036), the reader's day, month and lifetime never count them, and they are held instead by the
reader's background guards (``HOSTED_READER_BACKGROUND_USD_PER_DAY`` and ``..._PER_MONTH``) and by
the budget of every reader's background work for the day (``HOSTED_BACKGROUND_USD_PER_DAY``),
checked call by call. The platform's day counts them as it counts every call. Calls in flight are
counted on each side apart.

A reader's two free weeks (ADR 0045 step 1, migration 0037) start at their first question
(``start_free_weeks``, called by the ask routes). While they last the reader's day and month are
not checked; one ceiling is instead, ``HOSTED_FREE_USD`` of their own chat spend since the start,
read by ``reader_plan_state`` in the same transaction under the same lock and counted as the day
and month are (the conversation only, the ledger and reservations left open). A reply admitted
under it is finished as under the day. Everything else holds as for every reader: the lifetime
cap, the calls in flight, the platform's day. After the two weeks the day and month are back. A
reader whose ``owner_limits`` row sets their own day or month cap is held by those caps instead,
free weeks or not: an operator's word about one reader wins. Background work is never read for
the ceiling and never stopped by it.

A gifted reader (ADR 0045 step 2, migration 0038: the owner's own account and test accounts,
given a gift by the operator's command) is a paying reader: ``reader_plan_state`` answers
``paying`` while the gift lasts, the free ceiling is not applied to them, and they are held by
the reader's day and month (the paying readers' hidden guards) with today's lines, or by their own
``owner_limits`` caps where those are set. Everything else holds as for every reader. A gift that
has passed leaves the reader where they would be without it.

Story memory is never a reader's spend (ADR 0044 section 5): a chapter's calls are reserved by a
store bound to the notes account (``NotesCatalog`` in ``store.py``) against the edition being
built, under the same rules, with the notes account's own caps: its ``owner_limits`` day cap, or
the notes ceiling (``HOSTED_STORY_MEMORY_USD_PER_DAY``) where that is NULL, and a month cap only
where its row sets one. The platform's day counts those calls as it counts every other.

A reservation counts as in flight for ``IN_FLIGHT_MINUTES``. One still open after that was left
by a process that died: it no longer holds a place among the calls in flight, and since nobody
knows what it cost, it is counted as spent, at its reserved size, on the day it was made.

The per-book ceilings the wrappers pass (``max_input_tokens``, ``max_output_tokens``,
``max_usd``) are accepted and not used: the owner pays, so the caps are the reader's and the
platform's. The per-call token ceiling is still checked by the wrappers before any reservation.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC
from decimal import ROUND_HALF_EVEN, Decimal

from app.catalog.catalog import HOME_SPEND, Catalog
from app.hosted.companion import OwnerBoundAdapter, as_uuid

_PURPOSE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CENT_FRACTION = Decimal("1e-10")          # NUMERIC(20,10)
_OWNER_SPEND_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,47))"
_DAY_START = "(date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
_MONTH_START = "(date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
# No companion call lasts this long: the writer and the checker both time out in seconds.
IN_FLIGHT_MINUTES = 15
_IN_FLIGHT_SINCE = f"(now() - interval '{IN_FLIGHT_MINUTES} minutes')"


# The reader's companion day and month as they count against the caps: the ledger's companion
# rows since the start of the UTC day (month), plus companion reservations still open after
# IN_FLIGHT_MINUTES that were made in it. Of one side only: the reader's own conversation
# (``background`` false), which their allowance counts, or Mothli's background work for them
# (``background`` true), which only their guards count (migration 0036). One definition, read by
# ``reserve_cost`` and, for the background side, by the worker's claim (``app.hosted.jobs``),
# which moves a guarded reader's background job to the next UTC day. Built when it is used, from
# the module's current bounds.
def _spent(background: bool) -> str:
    side = "background" if background else "NOT background"
    return f"""
    COALESCE((SELECT sum(usd) FROM public.cost_ledger
      WHERE owner_id=%(o)s AND purpose IS NOT NULL AND {side}
        AND recorded_at >= {_DAY_START}),0)
    + COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
      WHERE owner_id=%(o)s AND state='reserved' AND purpose IS NOT NULL AND {side}
        AND created_at <= {_IN_FLIGHT_SINCE}
        AND created_at >= {_DAY_START}),0) AS day,
    COALESCE((SELECT sum(usd) FROM public.cost_ledger
      WHERE owner_id=%(o)s AND purpose IS NOT NULL AND {side}
        AND recorded_at >= {_MONTH_START}),0)
    + COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
      WHERE owner_id=%(o)s AND state='reserved' AND purpose IS NOT NULL AND {side}
        AND created_at <= {_IN_FLIGHT_SINCE}
        AND created_at >= {_MONTH_START}),0) AS month"""


@dataclass(frozen=True)
class SpendLimits:
    """The caps from the environment (``HOSTED_READER_USD_PER_DAY``, ``..._PER_MONTH``,
    ``HOSTED_PLATFORM_USD_PER_DAY``, ``HOSTED_STORY_MEMORY_USD_PER_DAY``,
    ``HOSTED_BACKGROUND_USD_PER_DAY``, ``HOSTED_READER_BACKGROUND_USD_PER_DAY`` and
    ``..._PER_MONTH``). The reader's two are defaults for a reader whose ``owner_limits`` row
    leaves its own cap NULL; the notes ceiling is the notes account's day cap where its row leaves
    its own NULL (ADR 0044 section 5). The last three hold Mothli's background work for readers
    (ADR 0041 section 6, amended): all readers' together in a UTC day, and one reader's in a UTC
    day and month. No reader ever sees them."""

    # A paying reader's hidden guards (ADR 0045 section 4, the owner's numbers).
    reader_usd_per_day: Decimal = Decimal("1.00")
    reader_usd_per_month: Decimal = Decimal("4.00")
    platform_usd_per_day: Decimal = Decimal("15.00")
    notes_usd_per_day: Decimal = Decimal("10.00")
    background_usd_per_day: Decimal = Decimal("3.00")
    reader_background_usd_per_day: Decimal = Decimal("1.00")
    reader_background_usd_per_month: Decimal = Decimal("5.00")
    # ADR 0045 step 1: a reader's two free weeks (``HOSTED_FREE_DAYS``) and their one ceiling, in
    # dollars of their own chat spend since the start (``HOSTED_FREE_USD``).
    free_usd: Decimal = Decimal("2.10")
    free_days: int = 14
    # ADR 0045 step 3: a subscriber's first paid month (``HOSTED_FIRST_PAID_MONTH_USD``), in place
    # of the month for the 30 days after their first Paddle plan began, once per account.
    first_paid_month_usd: Decimal = Decimal("6.30")


_limits_guard = threading.Lock()
_limits = SpendLimits()


def _money(value) -> Decimal:
    """A dollar amount as stored, to the column's ten places, rounded as Python's ``round`` (the
    difference from the local float is under $0.0000000001 and is never compounded)."""
    return Decimal(str(value)).quantize(_CENT_FRACTION, rounding=ROUND_HALF_EVEN)


def set_spend_limits(*, reader_usd_per_day, reader_usd_per_month, platform_usd_per_day,
                     notes_usd_per_day=SpendLimits.notes_usd_per_day,
                     background_usd_per_day=SpendLimits.background_usd_per_day,
                     reader_background_usd_per_day=SpendLimits.reader_background_usd_per_day,
                     reader_background_usd_per_month=SpendLimits.reader_background_usd_per_month,
                     free_usd=SpendLimits.free_usd, free_days=SpendLimits.free_days,
                     first_paid_month_usd=SpendLimits.first_paid_month_usd,
                     ) -> None:
    """Set this process's caps from settings (startup only, like the connection limit)."""
    global _limits
    values = [_money(value) for value in (
        reader_usd_per_day, reader_usd_per_month, platform_usd_per_day, notes_usd_per_day,
        background_usd_per_day, reader_background_usd_per_day, reader_background_usd_per_month,
        free_usd, first_paid_month_usd)]
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("spend caps must be finite and non-negative")
    if not isinstance(free_days, int) or isinstance(free_days, bool) or free_days < 1:
        raise ValueError("the free weeks must last at least one day")
    with _limits_guard:
        *values, first_paid_month = values
        _limits = SpendLimits(*values, free_days, first_paid_month)


def spend_limits() -> SpendLimits:
    return _limits


def _reader_cap_reached(used, policy, limits, *, notes: bool = False,
                        first_month=None) -> str | None:
    """"day" or "month" when that companion allowance of the owner is used up, else None: the
    owner's own ``owner_limits`` cap, or the process default where it is NULL. For the notes
    account (``notes``) the default day cap is the notes ceiling and there is no default month
    cap (ADR 0044 section 5): only one its row sets. In a subscriber's first paid month
    (``first_month``: their chat spend since their first Paddle plan began, which
    ``reader_plan_state`` answers only within the 30 days after it) the default month is that
    spend against the first paid month's ceiling, in place of the calendar month; a month cap of
    their own still wins, and the day is the day."""
    day_cap = policy["max_spend_usd_per_day"]
    month_cap = policy["max_spend_usd_per_month"]
    if day_cap is None:
        day_cap = limits.notes_usd_per_day if notes else limits.reader_usd_per_day
    month_used = used["month"]
    if month_cap is None and not notes:
        month_cap = limits.reader_usd_per_month
        if first_month is not None:
            month_cap, month_used = limits.first_paid_month_usd, first_month
    if used["day"] >= day_cap:
        return "day"
    if month_cap is not None and month_used >= month_cap:
        return "month"
    return None


def _background_guard_reached(used, limits) -> str | None:
    """"background_day" or "background_month" when this reader's background work (Mothli's cost)
    has used up their guard, else None. Set far above what reading uses (ADR 0041 section 6,
    amended): it is there for someone who would make Mothli spend without bound."""
    if used["day"] >= limits.reader_background_usd_per_day:
        return "background_day"
    if used["month"] >= limits.reader_background_usd_per_month:
        return "background_month"
    return None


def background_guard_reached(conn, owner_id) -> str | None:
    """"background_day" or "background_month" when ``reserve_cost`` would refuse this owner's
    next background call for that guard, else None. Read on the caller's connection, which must
    return rows as dicts: the worker's claim calls it under the owner's spend lock. The reader's
    own allowance (their conversation) is never read here."""
    row = conn.execute(f"SELECT {_spent(True)}", {"o": owner_id}).fetchone()
    return _background_guard_reached(row, _limits)


class AllowanceReached(RuntimeError):
    """A companion call refused before any money was spent. ``code`` is what the stream's error
    event carries; the message is written for the reader and names no amount and no setting."""

    def __init__(self, limit: str, message: str, *, code: str = "allowance_reached") -> None:
        super().__init__(message)
        self.limit = limit
        self.code = code
        self.reader_message = message


class ReaderAllowanceReached(AllowanceReached):
    """This reader's day, month or lifetime allowance, or their calls in flight."""


class PlatformAllowanceReached(AllowanceReached):
    """The platform's own day: every reader waits until the next UTC day."""


class BackgroundAllowanceReached(AllowanceReached):
    """Mothli's background work for a reader held: their guard for the day or month, or the
    budget of every reader's background work for the day. Never shown to a reader: the work
    waits for the next UTC day and the chat goes on."""


# Said by Mothli itself, as it speaks in the chat: the first person, and no amount or setting.
_DAY = ("We’ve talked a lot today, so I’m resting until tomorrow. "
        "Your book and your place are right here.")
_MONTH = ("We’ve talked our way through this month, so I’ll be back at the start of the next. "
          "Your book and your place are right here.")
_LIFETIME = ("We’ve used up the conversations this account has. "
             "Your books and your place are right here.")
_BUSY = "Give me a moment, I’m still on your last question. Then ask again."
# ADR 0045 section 6, "Ended at the free ceiling", without its button until the plan exists: the
# free weeks are not over, so it must not say they are. No number, no counter, never "unlimited".
_FREE = ("We’ve talked so much these first days that your free start with me is all used up. "
         "That’s a good sign! Your books, your place and your highlights stay right here, free, "
         "always.")
_PLATFORM = ("I’m resting for the rest of today and I’ll be back tomorrow. "
             "Your book and your place are right here.")
# For the logs only: background work is never mentioned to a reader.
_BACKGROUND = "background work waits for the next UTC day"


def _check_tokens(value, what):
    Catalog._check_cost_value(value, what)


def _settled_purpose(phase, fallback):
    """The purpose a settlement is recorded under. Never fails: a settlement after a provider
    call must not be lost to a name, so an unusual one is folded into the allowed shape."""
    if phase is None:
        return fallback
    if isinstance(phase, str) and _PURPOSE.match(phase):
        return phase
    folded = re.sub(r"[^a-z0-9-]+", "-", str(phase).casefold()).strip("-")[:64]
    return folded if _PURPOSE.match(folded) else fallback


def _reservation_id(value) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"unknown cost reservation {value!r}") from exc


def _number(value):
    return None if value is None else float(value)


class SpendStore(OwnerBoundAdapter):
    __slots__ = ()
    # Whether this store pays for story memory as the notes account (``NotesCatalog``), whose
    # default caps are the notes ceiling's rather than a reader's.
    _NOTES = False
    # Whether this store pays for Mothli's background work for its reader (the worker's catalog
    # for chapter notes and recollections, ``app.hosted.companion.jobs``): marked so, held by the
    # background guards and budget, and never counted against the reader's own allowance.
    _BACKGROUND = False

    # ---- which spend a book id names --------------------------------------------------------
    def _spend_book(self, conn, book_id):
        """(book id, incarnation) of a live book of this owner, or (None, None) for the home
        conversation. An unknown, foreign or deleted book is refused before anything is spent."""
        if book_id == HOME_SPEND:
            return None, None
        try:
            book = as_uuid(book_id)
        except LookupError as exc:
            raise ValueError(f"unknown book {book_id!r}") from exc
        row = conn.execute(
            "SELECT id,incarnation FROM public.books"
            " WHERE owner_id=%s AND id=%s AND deleted_at IS NULL",
            (self._owner.value, book)).fetchone()
        if row is None:
            raise ValueError(f"unknown book {book_id!r}")
        return row["id"], row["incarnation"]

    def _held(self, conn, book_id, reservation_id):
        """This owner's open companion reservation, locked, or ValueError as locally."""
        reservation = _reservation_id(reservation_id)
        if book_id == HOME_SPEND:
            book_clause, params = "book_id IS NULL", ()
        else:
            try:
                book_clause, params = "book_id=%s", (as_uuid(book_id),)
            except LookupError as exc:
                raise ValueError(f"unknown cost reservation {reservation_id!r}") from exc
        row = conn.execute(
            f"""SELECT id,book_id,book_incarnation,chapter_ordinal,phase,model,purpose,
                       reserved_input_tokens,reserved_output_tokens,reserved_usd,idempotency_key,
                       background,(created_at AT TIME ZONE 'UTC')::date AS spend_day
                FROM public.cost_reservations
                WHERE owner_id=%s AND id=%s AND {book_clause}
                  AND state='reserved' AND purpose IS NOT NULL
                FOR UPDATE""",
            (self._owner.value, reservation, *params)).fetchone()
        if row is None:
            raise ValueError(f"unknown cost reservation {reservation_id!r}")
        return row

    # ---- the two free weeks (ADR 0045 step 1) -----------------------------------------------
    def start_free_weeks(self) -> None:
        """The reader's first question: stamp the start of their free weeks, once. The database
        decides the account (the one this transaction is set to) and the time (its own clock);
        a start already stamped is kept."""
        with self._transaction() as conn:
            conn.execute("SELECT public.start_free_weeks()")

    def _plan(self, conn, policy, limits) -> tuple[str | None, object]:
        """(free weeks, first paid month), read in the caller's transaction, under the owner's
        spend lock, from ``reader_plan_state``.

        The free weeks: "open" while the reader's free weeks last, "used" once their own chat
        since the start has reached the free ceiling within them, None when the free weeks do not
        apply and the reader's day and month caps do: before the first question, after the two
        weeks, while they pay (a gift or Paddle), for the notes account, and for a reader whose
        ``owner_limits`` row sets their own day or month cap (an operator's word about one reader
        wins over the free weeks).

        The first paid month (ADR 0045 step 3): their chat spend since their first Paddle plan
        began, within the 30 days after it and while a Paddle plan pays, else None."""
        if self._NOTES:
            return None, None
        plan = conn.execute(
            "SELECT state,why,first_month_spent_usd FROM public.reader_plan_state(%s,%s,%s)",
            (self._owner.value, limits.free_days, limits.free_usd)).fetchone()
        first_month = plan["first_month_spent_usd"]
        if policy["max_spend_usd_per_day"] is not None \
                or policy["max_spend_usd_per_month"] is not None:
            return None, first_month
        if plan["state"] == "free":
            return "open", first_month
        return ("used" if plan["why"] == "ceiling" else None), first_month

    # ---- the four calls the wrappers make ---------------------------------------------------
    def reserve_cost(self, book_id, *, phase, model, input_tokens, output_tokens, usd,
                     max_input_tokens, max_output_tokens, max_usd, chapter_ordinal=None,
                     expected_reading_scope=None, max_preparation_usd=None):
        """Reserve one call, unless this reader's or the platform's allowance is used up.

        Returns the reservation id. Raises ``ReaderAllowanceReached`` or
        ``PlatformAllowanceReached`` (both ``RuntimeError``) at a cap, ``ValueError`` for an
        unknown book or a malformed value; nothing is reserved then. A later call of a reply
        already admitted (module docstring) is refused only for the calls in flight.

        A background store's call (``_BACKGROUND``) is Mothli's: the reader's day, month and
        lifetime allowance are not read, and its own guards and the budget of all readers'
        background work refuse it instead, with ``BackgroundAllowanceReached``. The calls in
        flight are counted on each side apart, so background work never makes the chat wait.

        In the reader's free weeks the free ceiling stands in for their day and month (module
        docstring); it only admits or refuses the call, as they do.
        """
        for value, what in ((input_tokens, "input_tokens"), (output_tokens, "output_tokens"),
                            (max_input_tokens, "max_input_tokens"),
                            (max_output_tokens, "max_output_tokens")):
            _check_tokens(value, what)
        Catalog._check_usd(usd)
        Catalog._check_usd(max_usd, "max_usd")
        if expected_reading_scope is not None or max_preparation_usd is not None:
            raise ValueError("reader preparation is not available in hosted mode")
        if not isinstance(phase, str) or not _PURPOSE.match(phase):
            raise ValueError(f"cost phase {phase!r} is not a hosted purpose")
        if not isinstance(model, str) or not model:
            raise ValueError("billing model must be a nonempty string")
        if chapter_ordinal is not None and (not isinstance(chapter_ordinal, int)
                                            or isinstance(chapter_ordinal, bool)
                                            or not 0 < chapter_ordinal < 2**31):
            raise ValueError(f"chapter_ordinal must be a positive int, got {chapter_ordinal!r}")
        amount = _money(usd)
        # Jev's models are named jev-<version> (config); every other call is the writer's.
        kind = "judge" if model.startswith("jev-") else "synthesis"
        limits = _limits
        admitted = getattr(self, "_admitted", None)       # set on a ReplyCatalog only
        under_way = admitted is not None and admitted.is_set()
        background = self._BACKGROUND
        side = "background" if background else "NOT background"
        reservation = uuid.uuid4()
        owner = self._owner.value
        with self._transaction() as conn:
            conn.execute(_OWNER_SPEND_LOCK, (owner,))
            book, incarnation = self._spend_book(conn, book_id)
            policy = conn.execute(
                """SELECT max_spend_usd,max_spend_usd_per_day,max_spend_usd_per_month,
                          max_companion_calls_in_flight
                   FROM public.owner_limits WHERE owner_id=%s""", (owner,)).fetchone()
            if policy is None:
                raise RuntimeError("owner limit policy is unavailable")
            used = conn.execute(
                f"""SELECT
                      (SELECT count(*) FROM public.cost_reservations
                        WHERE owner_id=%(o)s AND state='reserved' AND purpose IS NOT NULL
                          AND {side} AND created_at > {_IN_FLIGHT_SINCE}) AS in_flight,
                      COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
                        WHERE owner_id=%(o)s AND state='reserved' AND NOT background),0)
                        AS held_all,
                      {_spent(background)},
                      COALESCE((SELECT sum(usd) FROM public.cost_ledger
                        WHERE owner_id=%(o)s AND NOT background),0) AS lifetime""",
                {"o": owner}).fetchone()
            lifetime_cap = policy["max_spend_usd"]
            if background:
                guard = _background_guard_reached(used, limits)
                if guard is not None:
                    raise BackgroundAllowanceReached(guard, _BACKGROUND)
                reached = None
            else:
                # In the free weeks one ceiling stands in for the day and the month (ADR 0045).
                # A subscriber's first paid month stands in for their calendar month.
                free, first_month = self._plan(conn, policy, limits)
                reached = (None if free == "open" else "free" if free == "used"
                           else _reader_cap_reached(used, policy, limits, notes=self._NOTES,
                                                    first_month=first_month))
            if under_way or background:
                pass            # admitted with its first call: it finishes; or not the reader's
            elif reached == "day":
                raise ReaderAllowanceReached("day", _DAY)
            elif reached == "month":
                raise ReaderAllowanceReached("month", _MONTH)
            elif reached == "free":
                raise ReaderAllowanceReached("free", _FREE)
            elif (lifetime_cap is not None
                  and used["lifetime"] + used["held_all"] + amount > lifetime_cap):
                raise ReaderAllowanceReached("lifetime", _LIFETIME)
            if used["in_flight"] >= policy["max_companion_calls_in_flight"]:
                raise ReaderAllowanceReached("calls_in_flight", _BUSY, code="companion_busy")
            if background and not conn.execute(
                    "SELECT public.companion_background_open(%s) AS open",
                    (limits.background_usd_per_day,)).fetchone()["open"]:
                raise BackgroundAllowanceReached("background_budget", _BACKGROUND)
            if under_way:
                # Recorded against the platform's day like any call, but not refused by it.
                conn.execute(
                    "SELECT public.companion_platform_adjust((now() AT TIME ZONE 'UTC')::date,%s,0)",
                    (amount,))
            else:
                fits = conn.execute("SELECT public.companion_platform_reserve(%s,%s) AS fits",
                                    (amount, limits.platform_usd_per_day)).fetchone()["fits"]
                if not fits:
                    raise PlatformAllowanceReached("platform_day", _PLATFORM)
            conn.execute(
                """INSERT INTO public.cost_reservations
                     (owner_id,id,book_id,book_incarnation,chapter_ordinal,phase,model,
                      reserved_input_tokens,reserved_output_tokens,reserved_usd,idempotency_key,
                      purpose,background)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (owner, reservation, book, incarnation, chapter_ordinal, kind, model,
                 input_tokens, output_tokens, amount, f"companion:{reservation.hex}", phase,
                 background))
        if admitted is not None:
            admitted.set()
        return reservation.hex

    def note_reservation_actual(self, book_id, reservation_id, *, input_tokens, output_tokens,
                                usd, model=None):
        """Replace a reservation's estimate with known actual usage while keeping it open."""
        _check_tokens(input_tokens, "input_tokens")
        _check_tokens(output_tokens, "output_tokens")
        Catalog._check_usd(usd)
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("actual billing model must be a nonempty string")
        amount = _money(usd)
        with self._transaction() as conn:
            conn.execute(_OWNER_SPEND_LOCK, (self._owner.value,))
            row = self._held(conn, book_id, reservation_id)
            conn.execute(
                """UPDATE public.cost_reservations
                   SET reserved_input_tokens=%s,reserved_output_tokens=%s,reserved_usd=%s,
                       actual_input_tokens=%s,actual_output_tokens=%s,actual_usd=%s,
                       model=COALESCE(%s,model)
                   WHERE owner_id=%s AND id=%s AND state='reserved'""",
                (input_tokens, output_tokens, amount, input_tokens, output_tokens, amount, model,
                 self._owner.value, row["id"]))
            conn.execute("SELECT public.companion_platform_adjust(%s,%s,0)",
                         (row["spend_day"], amount - row["reserved_usd"]))

    def settle_cost(self, book_id, reservation_id, *, phase=None):
        """Move one reservation into the ledger exactly once, at its reserved (or noted) size."""
        with self._transaction() as conn:
            conn.execute(_OWNER_SPEND_LOCK, (self._owner.value,))
            row = self._held(conn, book_id, reservation_id)
            conn.execute(
                """INSERT INTO public.cost_ledger
                     (owner_id,book_id,book_incarnation,chapter_ordinal,phase,model,input_tokens,
                      output_tokens,usd,idempotency_key,purpose,background)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (self._owner.value, row["book_id"], row["book_incarnation"],
                 row["chapter_ordinal"], row["phase"], row["model"],
                 row["reserved_input_tokens"], row["reserved_output_tokens"],
                 row["reserved_usd"], row["idempotency_key"],
                 _settled_purpose(phase, row["purpose"]), row["background"]))
            conn.execute(
                """UPDATE public.cost_reservations SET state='settled',settled_at=now()
                   WHERE owner_id=%s AND id=%s AND state='reserved'""",
                (self._owner.value, row["id"]))
            conn.execute("SELECT public.companion_platform_adjust(%s,%s,%s)",
                         (row["spend_day"], -row["reserved_usd"], row["reserved_usd"]))

    def discard_cost_reservation(self, book_id, reservation_id):
        """Release a proven-zero reservation: the provider reported zero usage, or refused the
        call outright before doing any work. Never for a failure whose spend is unknown."""
        with self._transaction() as conn:
            conn.execute(_OWNER_SPEND_LOCK, (self._owner.value,))
            row = self._held(conn, book_id, reservation_id)
            conn.execute(
                """UPDATE public.cost_reservations SET state='released',settled_at=now()
                   WHERE owner_id=%s AND id=%s AND state='reserved'""",
                (self._owner.value, row["id"]))
            conn.execute("SELECT public.companion_platform_adjust(%s,%s,0)",
                         (row["spend_day"], -row["reserved_usd"]))

    # ---- what was spent, as the local catalog shows it --------------------------------------
    def _listed_book(self, conn, book_id):
        """("book_id IS NULL", ()) for home, the predicate for a live book, or None."""
        if book_id == HOME_SPEND:
            return "book_id IS NULL", ()
        try:
            book = as_uuid(book_id)
        except LookupError:
            return None
        live = conn.execute(
            "SELECT 1 FROM public.books WHERE owner_id=%s AND id=%s AND deleted_at IS NULL",
            (self._owner.value, book)).fetchone()
        return None if live is None else ("book_id=%s", (book,))

    def get_costs(self, book_id):
        """Every ledger entry of one book (or the home conversation), oldest first."""
        with self._transaction() as conn:
            found = self._listed_book(conn, book_id)
            if found is None:
                return []
            clause, params = found
            rows = conn.execute(
                f"""SELECT chapter_ordinal,COALESCE(purpose,phase) AS phase,model,input_tokens,
                           output_tokens,usd,recorded_at
                    FROM public.cost_ledger WHERE owner_id=%s AND {clause}
                    ORDER BY recorded_at,id""",
                (self._owner.value, *params)).fetchall()
        return [{"chapter_ordinal": row["chapter_ordinal"], "phase": row["phase"],
                 "model": row["model"], "input_tokens": row["input_tokens"],
                 "output_tokens": row["output_tokens"], "usd": _number(row["usd"]),
                 "at": row["recorded_at"].astimezone(UTC).isoformat()} for row in rows]

    def get_cost_reservations(self, book_id):
        """Every open reservation of one book (or the home conversation), oldest first."""
        with self._transaction() as conn:
            found = self._listed_book(conn, book_id)
            if found is None:
                return []
            clause, params = found
            rows = conn.execute(
                f"""SELECT id,chapter_ordinal,COALESCE(purpose,phase) AS phase,model,
                           reserved_input_tokens,reserved_output_tokens,reserved_usd,
                           actual_input_tokens,actual_output_tokens,actual_usd,created_at
                    FROM public.cost_reservations
                    WHERE owner_id=%s AND {clause} AND state='reserved'
                    ORDER BY created_at,id""",
                (self._owner.value, *params)).fetchall()
        return [{"reservation_id": row["id"].hex, "chapter_ordinal": row["chapter_ordinal"],
                 "phase": row["phase"], "model": row["model"],
                 "reserved_input_tokens": row["reserved_input_tokens"],
                 "reserved_output_tokens": row["reserved_output_tokens"],
                 "reserved_usd": _number(row["reserved_usd"]),
                 "actual_input_tokens": row["actual_input_tokens"],
                 "actual_output_tokens": row["actual_output_tokens"],
                 "actual_usd": _number(row["actual_usd"]),
                 "at": row["created_at"].astimezone(UTC).isoformat()} for row in rows]
