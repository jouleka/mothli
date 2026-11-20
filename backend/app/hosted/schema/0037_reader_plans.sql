-- ADR 0045 step 1: a reader's two free weeks with Mothli. They start at the reader's first
-- question, from the database's clock, once per account; while they last the reader's day and
-- month allowance is not applied, and one ceiling in dollars of their own chat spend since the
-- start is applied instead (app/hosted/companion/spend.py).
--
-- One row per account, made on first need: no row means the reader has never asked Mothli
-- anything. Later steps add the gift and Paddle columns in their own migrations. The state is
-- never stored as a word: reader_plan_state works it out each time from the row, the clock and
-- the reader's own chat spend.
CREATE TABLE reader_plans (
  owner_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  free_started_at  TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE reader_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE reader_plans FORCE ROW LEVEL SECURITY;
CREATE POLICY reader_plans_owner_isolation ON reader_plans
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

-- The one way a reader's session writes the row: the tenant role holds SELECT only on the table.
-- It takes no argument, so no reader can pick a date or another account: the account is the one
-- the transaction is set to, the time is the database's, and a start already stamped is kept
-- (removing a book, clearing a conversation or asking again changes nothing).
CREATE FUNCTION start_free_weeks()
RETURNS void
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  INSERT INTO public.reader_plans AS plan (owner_id, free_started_at)
  SELECT reader.id, now()
  FROM (SELECT public.app_current_owner_id() AS id) AS reader
  WHERE reader.id IS NOT NULL
  ON CONFLICT (owner_id) DO UPDATE
    SET free_started_at = now(), updated_at = now()
    WHERE plan.free_started_at IS NULL;
$$;

REVOKE ALL ON FUNCTION start_free_weeks() FROM PUBLIC;

-- Where one reader stands: 'new' (never asked), 'free' (within free_days of the start and under
-- free_usd), or 'ended' with why: 'time' (free_days have passed) or 'ceiling' (free_usd of their
-- own chat spend since the start). Their chat spend is what the spend check counts for their day
-- and month (``_spent`` in spend.py, its reader side): the ledger's companion rows that are not
-- background work, and reservations still open after the 15 minutes a call can last
-- (``IN_FLIGHT_MINUTES``), counted as spent. Background work never counts here.
--
-- It runs as its caller: on the tenant role row-level security shows only the reader's own row
-- and spend, so asking about another account answers 'new' with nothing spent. Later steps add
-- the gift and Paddle's states to the answer.
CREATE FUNCTION reader_plan_state(reader UUID, free_days INTEGER, free_usd NUMERIC)
RETURNS TABLE (state TEXT, why TEXT, free_started_at TIMESTAMPTZ, free_ends_at TIMESTAMPTZ)
LANGUAGE sql
STABLE
STRICT
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH plan AS (
    SELECT found.free_started_at,
           found.free_started_at + make_interval(days => reader_plan_state.free_days) AS ends_at
    FROM public.reader_plans AS found
    WHERE found.owner_id = reader_plan_state.reader AND found.free_started_at IS NOT NULL
  ), spent AS (
    SELECT COALESCE((SELECT sum(entry.usd) FROM public.cost_ledger AS entry, plan
                     WHERE entry.owner_id = reader_plan_state.reader
                       AND entry.purpose IS NOT NULL AND NOT entry.background
                       AND entry.recorded_at >= plan.free_started_at), 0)
         + COALESCE((SELECT sum(held.reserved_usd) FROM public.cost_reservations AS held, plan
                     WHERE held.owner_id = reader_plan_state.reader
                       AND held.state = 'reserved' AND held.purpose IS NOT NULL
                       AND NOT held.background
                       AND held.created_at <= now() - interval '15 minutes'
                       AND held.created_at >= plan.free_started_at), 0) AS usd
  )
  SELECT CASE WHEN plan.free_started_at IS NULL THEN 'new'
              WHEN now() >= plan.ends_at OR spent.usd >= reader_plan_state.free_usd THEN 'ended'
              ELSE 'free' END,
         CASE WHEN plan.free_started_at IS NULL THEN NULL
              WHEN now() >= plan.ends_at THEN 'time'
              WHEN spent.usd >= reader_plan_state.free_usd THEN 'ceiling' END,
         plan.free_started_at,
         plan.ends_at
  FROM spent LEFT JOIN plan ON true;
$$;

REVOKE ALL ON FUNCTION reader_plan_state(UUID, INTEGER, NUMERIC) FROM PUBLIC;
