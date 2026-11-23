-- ADR 0045 step 2: gifts. The owner's own account and test accounts are paying readers without
-- Paddle: reader_plans.gift_until, set only through set_gift, which only the operator role may
-- execute (python -m app.hosted.plans). NULL is no gift; a far date (9999-12-31) is "for good".
-- A gift lasts to the end of its UTC day.
--
-- Nothing else changes: no existing row gets a gift, and the free weeks are stamped as before (a
-- gifted reader's first question still stamps them, once, so they cannot be had again after the
-- gift ends).
ALTER TABLE reader_plans ADD COLUMN gift_until DATE;

-- The one way a gift is written. It runs as the migration owner, so the operator role needs no
-- write on the table. With a date it makes the reader's row if they have none and sets the gift;
-- with NULL it ends the gift and makes no row. Answers whether the account exists (an unknown id
-- is given nothing).
CREATE FUNCTION set_gift(reader UUID, until DATE)
RETURNS boolean
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  INSERT INTO public.reader_plans AS plan (owner_id, gift_until)
  SELECT person.id, set_gift.until
  FROM public.users AS person
  WHERE person.id = set_gift.reader AND set_gift.until IS NOT NULL
  ON CONFLICT (owner_id) DO UPDATE
    SET gift_until = EXCLUDED.gift_until, updated_at = now();
  UPDATE public.reader_plans
    SET gift_until = NULL, updated_at = now()
    WHERE owner_id = set_gift.reader AND set_gift.until IS NULL AND gift_until IS NOT NULL;
  SELECT EXISTS (SELECT 1 FROM public.users WHERE id = set_gift.reader);
$$;

REVOKE ALL ON FUNCTION set_gift(UUID, DATE) FROM PUBLIC;

-- Where one reader stands, as in 0037, with the gift first: a reader whose gift lasts today is
-- 'paying', paid by 'gift' (a later step adds Paddle's plans as another paid_by). A gift that has
-- passed changes nothing: the reader is where they would otherwise be ('new', 'free' or
-- 'ended'). Also answered: the gift's date (live or passed, NULL for none) and the reader's own
-- chat spend since their free weeks began, counted as before (0 before they began). Its result
-- has new columns, so it is made again; the grants are made again by the deployment's role
-- script, which runs after the migrations.
DROP FUNCTION reader_plan_state(UUID, INTEGER, NUMERIC);

CREATE FUNCTION reader_plan_state(reader UUID, free_days INTEGER, free_usd NUMERIC)
RETURNS TABLE (state TEXT, why TEXT, free_started_at TIMESTAMPTZ, free_ends_at TIMESTAMPTZ,
               paid_by TEXT, gift_until DATE, free_spent_usd NUMERIC)
LANGUAGE sql
STABLE
STRICT
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH found AS (
    SELECT kept.free_started_at, kept.gift_until,
           kept.gift_until >= (now() AT TIME ZONE 'UTC')::date AS gifted
    FROM public.reader_plans AS kept
    WHERE kept.owner_id = reader_plan_state.reader
  ), plan AS (
    SELECT found.free_started_at,
           found.free_started_at + make_interval(days => reader_plan_state.free_days) AS ends_at
    FROM found
    WHERE found.free_started_at IS NOT NULL
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
  SELECT CASE WHEN found.gifted THEN 'paying'
              WHEN plan.free_started_at IS NULL THEN 'new'
              WHEN now() >= plan.ends_at OR spent.usd >= reader_plan_state.free_usd THEN 'ended'
              ELSE 'free' END,
         CASE WHEN found.gifted OR plan.free_started_at IS NULL THEN NULL
              WHEN now() >= plan.ends_at THEN 'time'
              WHEN spent.usd >= reader_plan_state.free_usd THEN 'ceiling' END,
         plan.free_started_at,
         plan.ends_at,
         CASE WHEN found.gifted THEN 'gift' END,
         found.gift_until,
         spent.usd
  FROM spent LEFT JOIN plan ON true LEFT JOIN found ON true;
$$;

REVOKE ALL ON FUNCTION reader_plan_state(UUID, INTEGER, NUMERIC) FROM PUBLIC;
