-- ADR 0041 section 6, amended 20 November 2026: a reader has one allowance, talking with Mothli.
-- What Mothli does in the background for a reader (their chapter notes, what it remembers of
-- them) is Mothli's cost: still in the reader's ledger, still on the platform's day, but never
-- counted against the reader's day, month or lifetime allowance. It is held instead by a guard
-- per reader and by one budget for all readers' background work together, each until the next
-- UTC day, without a word to the reader.
--
-- The mark is set when the call is reserved (by the worker's catalog, app/hosted/companion/
-- jobs.py) and carried into the ledger when it is settled. Story memory is not marked: it is the
-- notes account's own spend, under its own ceiling (ADR 0044 section 5).
ALTER TABLE cost_reservations ADD COLUMN background BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE cost_ledger ADD COLUMN background BOOLEAN NOT NULL DEFAULT false;

-- Background work already recorded is marked by the names it was recorded under, so a reader
-- whose notes were built earlier this month gets back what they used of their allowance.
UPDATE cost_reservations SET background = true WHERE purpose ~ '^(notes|recollect)(-|$)';
UPDATE cost_ledger SET background = true WHERE purpose ~ '^(notes|recollect)(-|$)';

CREATE INDEX ix_cost_ledger_background_time
  ON cost_ledger (recorded_at) WHERE background;
CREATE INDEX ix_cost_reservations_background_open
  ON cost_reservations (created_at) WHERE background AND state = 'reserved';

-- Whether every reader's background spend so far this UTC day (the ledger, and reservations still
-- open) is under ``day_cap``. Like the platform's two functions it answers yes or no, never a
-- sum, and the tenant role holds no privilege on the rows of other readers it adds up. It takes a
-- lock held to the end of the caller's transaction, so two readers' background calls cannot both
-- pass the budget's last cent: the caller reserves in that same transaction.
CREATE FUNCTION companion_background_open(day_cap NUMERIC)
RETURNS BOOLEAN
LANGUAGE sql
STRICT
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT pg_advisory_xact_lock(hashtextextended('companion-background-day', 47));
  SELECT day_cap >= 0
     AND COALESCE((SELECT sum(entry.usd) FROM public.cost_ledger AS entry
                   WHERE entry.background
                     AND entry.recorded_at >= (date_trunc('day', now() AT TIME ZONE 'UTC')
                                               AT TIME ZONE 'UTC')), 0)
       + COALESCE((SELECT sum(held.reserved_usd) FROM public.cost_reservations AS held
                   WHERE held.background AND held.state = 'reserved'
                     AND held.created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC')
                                             AT TIME ZONE 'UTC')), 0)
       < day_cap;
$$;

REVOKE ALL ON FUNCTION companion_background_open(NUMERIC) FROM PUBLIC;
