-- ADR 0046: the two free weeks are removed. Reading stays free; talking with Mothli is the plan
-- from the first question. A reader is 'new' (never had a plan or a gift), 'paying' (a gift that
-- lasts, or a live Paddle plan, exactly as in 0039) or 'ended' (they had a plan or a gift and it
-- stopped; why 'plan'). Nothing starts free weeks any more, so their start, their stamp and their
-- settings go; the first paid month and gifts are kept exactly as they are.
--
-- A reader who was in their free weeks, or whose free weeks were over, and who never had a plan
-- or a gift, is 'new' from here on.
DROP FUNCTION start_free_weeks();

DROP FUNCTION reader_plan_state(UUID, INTEGER, NUMERIC);

ALTER TABLE reader_plans DROP COLUMN free_started_at;

-- Where one reader stands, as in 0039 without the free weeks: a gift that lasts today wins; then
-- a live Paddle plan (active, trialing or past_due, including one cancelled to the end of what was
-- paid, until its scheduled end) is 'paying', paid by 'paddle'; a Paddle plan paused, cancelled
-- or past its scheduled end, or a gift that has passed, is 'ended' with why 'plan'; anyone else
-- is 'new'. Also answered, as in 0039: the gift's date, the plan's status, the end of its period,
-- its scheduled end, the first time a Paddle plan made the reader paying, and within the 30 days
-- after that, while a Paddle plan pays and no gift does, the reader's own chat spend since then,
-- which the spend check holds to HOSTED_FIRST_PAID_MONTH_USD in place of the calendar month. Its
-- arguments change, so it is made again; it runs as its caller (on the tenant role row-level
-- security shows only the reader's own row and spend), and the grants are made again by the
-- deployment's role script, which runs after the migrations.
CREATE FUNCTION reader_plan_state(reader UUID)
RETURNS TABLE (state TEXT, why TEXT, paid_by TEXT, gift_until DATE, paddle_status TEXT,
               period_ends_at TIMESTAMPTZ, plan_ends_at TIMESTAMPTZ, first_paid_at TIMESTAMPTZ,
               first_month_spent_usd NUMERIC)
LANGUAGE sql
STABLE
STRICT
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH found AS (
    SELECT kept.gift_until, kept.paddle_status, kept.period_ends_at, kept.ends_at,
           kept.first_paid_at,
           COALESCE(kept.gift_until >= (now() AT TIME ZONE 'UTC')::date, false) AS gifted,
           kept.gift_until IS NOT NULL OR kept.paddle_subscription_id IS NOT NULL AS had_plan,
           COALESCE(kept.paddle_status IN ('active', 'trialing', 'past_due')
                    AND (kept.ends_at IS NULL OR now() < kept.ends_at), false) AS paddle_live
    FROM public.reader_plans AS kept
    WHERE kept.owner_id = reader_plan_state.reader
  ), first_month AS (
    SELECT found.first_paid_at
    FROM found
    WHERE found.paddle_live AND NOT found.gifted
      AND now() < found.first_paid_at + interval '30 days'
  ), first_spent AS (
    SELECT first_month.first_paid_at,
           COALESCE((SELECT sum(entry.usd) FROM public.cost_ledger AS entry
                     WHERE entry.owner_id = reader_plan_state.reader
                       AND entry.purpose IS NOT NULL AND NOT entry.background
                       AND entry.recorded_at >= first_month.first_paid_at), 0)
         + COALESCE((SELECT sum(held.reserved_usd) FROM public.cost_reservations AS held
                     WHERE held.owner_id = reader_plan_state.reader
                       AND held.state = 'reserved' AND held.purpose IS NOT NULL
                       AND NOT held.background
                       AND held.created_at <= now() - interval '15 minutes'
                       AND held.created_at >= first_month.first_paid_at), 0) AS usd
    FROM first_month
  )
  SELECT CASE WHEN found.gifted OR found.paddle_live THEN 'paying'
              WHEN found.had_plan THEN 'ended'
              ELSE 'new' END,
         CASE WHEN found.gifted OR found.paddle_live THEN NULL
              WHEN found.had_plan THEN 'plan' END,
         CASE WHEN found.gifted THEN 'gift' WHEN found.paddle_live THEN 'paddle' END,
         found.gift_until,
         found.paddle_status,
         found.period_ends_at,
         found.ends_at,
         found.first_paid_at,
         first_spent.usd
  FROM (SELECT 1) AS one LEFT JOIN found ON true LEFT JOIN first_spent ON true;
$$;

REVOKE ALL ON FUNCTION reader_plan_state(UUID) FROM PUBLIC;
