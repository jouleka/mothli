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
reached; background work is checked call by call and has no such allowance.

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
# IN_FLIGHT_MINUTES that were made in it. One definition, read by ``reserve_cost`` and by the
# worker's claim (``app.hosted.jobs``), which skips a reader whose allowance is used up. Built
# when it is used, from the module's current bounds.
def _reader_spent() -> str:
    return f"""
    COALESCE((SELECT sum(usd) FROM public.cost_ledger
      WHERE owner_id=%(o)s AND purpose IS NOT NULL
        AND recorded_at >= {_DAY_START}),0)
    + COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
      WHERE owner_id=%(o)s AND state='reserved' AND purpose IS NOT NULL
        AND created_at <= {_IN_FLIGHT_SINCE}
        AND created_at >= {_DAY_START}),0) AS day,
    COALESCE((SELECT sum(usd) FROM public.cost_ledger
      WHERE owner_id=%(o)s AND purpose IS NOT NULL
        AND recorded_at >= {_MONTH_START}),0)
    + COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
      WHERE owner_id=%(o)s AND state='reserved' AND purpose IS NOT NULL
        AND created_at <= {_IN_FLIGHT_SINCE}
        AND created_at >= {_MONTH_START}),0) AS month"""


@dataclass(frozen=True)
class SpendLimits:
    """The caps from the environment (``HOSTED_READER_USD_PER_DAY``, ``..._PER_MONTH``,
    ``HOSTED_PLATFORM_USD_PER_DAY``). The reader's two are defaults for a reader whose
    ``owner_limits`` row leaves its own cap NULL. The amounts are to be confirmed by the owner."""

    reader_usd_per_day: Decimal = Decimal("0.10")
    reader_usd_per_month: Decimal = Decimal("2.00")
    platform_usd_per_day: Decimal = Decimal("5.00")


_limits_guard = threading.Lock()
_limits = SpendLimits()


def _money(value) -> Decimal:
    """A dollar amount as stored, to the column's ten places, rounded as Python's ``round`` (the
    difference from the local float is under $0.0000000001 and is never compounded)."""
    return Decimal(str(value)).quantize(_CENT_FRACTION, rounding=ROUND_HALF_EVEN)


def set_spend_limits(*, reader_usd_per_day, reader_usd_per_month, platform_usd_per_day) -> None:
    """Set this process's caps from settings (startup only, like the connection limit)."""
    global _limits
    values = [_money(value) for value in (reader_usd_per_day, reader_usd_per_month,
                                          platform_usd_per_day)]
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("spend caps must be finite and non-negative")
    with _limits_guard:
        _limits = SpendLimits(*values)


def spend_limits() -> SpendLimits:
    return _limits


def _reader_cap_reached(used, policy, limits) -> str | None:
    """"day" or "month" when that companion allowance of the reader is used up, else None: the
    reader's own ``owner_limits`` cap, or the process default where it is NULL."""
    day_cap = policy["max_spend_usd_per_day"]
    month_cap = policy["max_spend_usd_per_month"]
    day_cap = limits.reader_usd_per_day if day_cap is None else day_cap
    month_cap = limits.reader_usd_per_month if month_cap is None else month_cap
    if used["day"] >= day_cap:
        return "day"
    if used["month"] >= month_cap:
        return "month"
    return None


def reader_cap_reached(conn, owner_id) -> str | None:
    """"day" or "month" when ``reserve_cost`` would refuse this owner's next call for that cap
    (a reply already under way aside), else None. Read on the caller's connection, which must
    return rows as dicts: the worker's claim calls it under the owner's spend lock. Raises
    ``LookupError`` when the owner has no limit policy."""
    row = conn.execute(
        f"""SELECT policy.max_spend_usd_per_day,policy.max_spend_usd_per_month,{_reader_spent()}
            FROM public.owner_limits AS policy WHERE policy.owner_id=%(o)s""",
        {"o": owner_id}).fetchone()
    if row is None:
        raise LookupError("owner limit policy is unavailable")
    return _reader_cap_reached(row, row, _limits)


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


# Said by Mothli itself, as it speaks in the chat: the first person, and no amount or setting.
_DAY = ("We’ve talked a lot today, so I’m resting until tomorrow. "
        "Your book and your place are right here.")
_MONTH = ("We’ve talked our way through this month, so I’ll be back at the start of the next. "
          "Your book and your place are right here.")
_LIFETIME = ("We’ve used up the conversations this account has. "
             "Your books and your place are right here.")
_BUSY = "Give me a moment, I’m still on your last question. Then ask again."
_PLATFORM = ("I’m resting for the rest of today and I’ll be back tomorrow. "
             "Your book and your place are right here.")


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
                       (created_at AT TIME ZONE 'UTC')::date AS spend_day
                FROM public.cost_reservations
                WHERE owner_id=%s AND id=%s AND {book_clause}
                  AND state='reserved' AND purpose IS NOT NULL
                FOR UPDATE""",
            (self._owner.value, reservation, *params)).fetchone()
        if row is None:
            raise ValueError(f"unknown cost reservation {reservation_id!r}")
        return row

    # ---- the four calls the wrappers make ---------------------------------------------------
    def reserve_cost(self, book_id, *, phase, model, input_tokens, output_tokens, usd,
                     max_input_tokens, max_output_tokens, max_usd, chapter_ordinal=None,
                     expected_reading_scope=None, max_preparation_usd=None):
        """Reserve one call, unless this reader's or the platform's allowance is used up.

        Returns the reservation id. Raises ``ReaderAllowanceReached`` or
        ``PlatformAllowanceReached`` (both ``RuntimeError``) at a cap, ``ValueError`` for an
        unknown book or a malformed value; nothing is reserved then. A later call of a reply
        already admitted (module docstring) is refused only for the calls in flight.
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
                          AND created_at > {_IN_FLIGHT_SINCE}) AS in_flight,
                      COALESCE((SELECT sum(reserved_usd) FROM public.cost_reservations
                        WHERE owner_id=%(o)s AND state='reserved'),0) AS held_all,
                      {_reader_spent()},
                      COALESCE((SELECT sum(usd) FROM public.cost_ledger
                        WHERE owner_id=%(o)s),0) AS lifetime""",
                {"o": owner}).fetchone()
            reached = _reader_cap_reached(used, policy, limits)
            lifetime_cap = policy["max_spend_usd"]
            if under_way:
                pass                                   # admitted with its first call: it finishes
            elif reached == "day":
                raise ReaderAllowanceReached("day", _DAY)
            elif reached == "month":
                raise ReaderAllowanceReached("month", _MONTH)
            elif (lifetime_cap is not None
                  and used["lifetime"] + used["held_all"] + amount > lifetime_cap):
                raise ReaderAllowanceReached("lifetime", _LIFETIME)
            if used["in_flight"] >= policy["max_companion_calls_in_flight"]:
                raise ReaderAllowanceReached("calls_in_flight", _BUSY, code="companion_busy")
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
                      purpose)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (owner, reservation, book, incarnation, chapter_ordinal, kind, model,
                 input_tokens, output_tokens, amount, f"companion:{reservation.hex}", phase))
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
                      output_tokens,usd,idempotency_key,purpose)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (self._owner.value, row["book_id"], row["book_incarnation"],
                 row["chapter_ordinal"], row["phase"], row["model"],
                 row["reserved_input_tokens"], row["reserved_output_tokens"],
                 row["reserved_usd"], row["idempotency_key"],
                 _settled_purpose(phase, row["purpose"])))
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
