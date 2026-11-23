-- ADR 0045 step 3: Paddle's messages. A message whose signature the server has checked, about one
-- of Mothli's own prices, can make a reader a paying one (app/hosted/billing.py). Paddle's
-- account is shared with another product of the owner's: a message about any other price is
-- answered and kept nowhere, so it never reaches this table.
--
-- The subscription's part of a reader's row, as section 1 of the ADR lists it. Written only by
-- the billing role, for the account the transaction is set to (row-level security, as for the
-- tenant role): NULL until a Paddle message about one of Mothli's prices names the reader.
ALTER TABLE reader_plans
  ADD COLUMN paddle_customer_id TEXT CHECK (paddle_customer_id ~ '^ctm_[a-z0-9]{26}$'),
  ADD COLUMN paddle_subscription_id TEXT UNIQUE
    CHECK (paddle_subscription_id ~ '^sub_[a-z0-9]{26}$'),
  ADD COLUMN paddle_status TEXT
    CHECK (paddle_status IN ('active', 'trialing', 'past_due', 'paused', 'canceled')),
  ADD COLUMN period_ends_at TIMESTAMPTZ,
  ADD COLUMN ends_at TIMESTAMPTZ,
  ADD COLUMN paddle_occurred_at TIMESTAMPTZ,
  -- The first time a Paddle plan made this reader paying (active or past_due), from the database
  -- clock: kept once per account and never moved, by a cancel, a new plan or a gift. It starts
  -- their first paid month (ADR 0045 section 4), which they have once.
  ADD COLUMN first_paid_at TIMESTAMPTZ,
  ADD CONSTRAINT reader_plans_paddle_whole CHECK (
    (paddle_subscription_id IS NULL) = (paddle_status IS NULL)
    AND (paddle_subscription_id IS NULL) = (paddle_occurred_at IS NULL));

-- Every Paddle message about one of Mothli's prices, once: its id (so a repeat is answered and
-- ignored), its type, when Paddle says it happened, the account it was for if one was found, and
-- what was done with it. No payload, no customer, no amount. The account is NULL for a message
-- naming no account, or one unknown or erased; the purge takes an erased account's rows with
-- every other table that has an owner_id.
CREATE TABLE paddle_events (
  event_id     TEXT PRIMARY KEY CHECK (event_id ~ '^evt_[a-z0-9]{26}$'),
  event_type   TEXT NOT NULL CHECK (event_type ~ '^subscription\.[a-z_]{1,32}$'),
  occurred_at  TIMESTAMPTZ NOT NULL,
  owner_id     UUID REFERENCES users(id),
  outcome      TEXT NOT NULL
    CHECK (outcome IN ('applied', 'older', 'other_subscription', 'no_account')),
  received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE paddle_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE paddle_events FORCE ROW LEVEL SECURITY;
CREATE POLICY paddle_events_owner_isolation ON paddle_events
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id IS NULL OR owner_id = app_current_owner_id());

-- Which account a message is for, the one look across accounts the billing role makes: the
-- account that holds this subscription if one does, else the account the message names in its
-- custom data. NULL for neither, or for an account that has been erased. It runs as the migration
-- owner, so the billing role needs no right on users and sees no other reader's row; it answers
-- one id and nothing else. Only the billing role may execute it (scripts/hosted_roles.py).
CREATE FUNCTION paddle_reader(subscription TEXT, named UUID)
RETURNS UUID
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT person.id
  FROM public.users AS person
  WHERE person.status <> 'deleted'
    AND person.id = COALESCE(
      (SELECT plan.owner_id FROM public.reader_plans AS plan
       WHERE plan.paddle_subscription_id = paddle_reader.subscription),
      paddle_reader.named);
$$;

REVOKE ALL ON FUNCTION paddle_reader(TEXT, UUID) FROM PUBLIC;

-- Where one reader stands, as in 0038, with Paddle's plan: a gift that lasts today wins; then a
-- live Paddle plan is 'paying', paid by 'paddle': active, trialing, or past_due (Paddle is
-- retrying a payment and says to keep access), including a plan cancelled to the end of what was
-- paid, which Paddle keeps active with a scheduled change until then (ends_at); a plan paused or
-- cancelled, or past its scheduled end, is 'ended' with why 'plan'. A reader who had a Paddle
-- plan and stopped is never back in the free weeks. Also answered: the plan's status, the end of
-- its period, and its scheduled end. And the first paid month (section 4): within 30 days of the
-- first time a Paddle plan made the reader paying, while a Paddle plan pays (not a gift), the
-- reader's own chat spend since that time (counted as the free weeks' is), which the spend check
-- holds to HOSTED_FIRST_PAID_MONTH_USD in place of the calendar month; NULL otherwise. Its result
-- has new columns, so it is made again; the grants are made again by the deployment's role script,
-- which runs after the migrations.
DROP FUNCTION reader_plan_state(UUID, INTEGER, NUMERIC);

CREATE FUNCTION reader_plan_state(reader UUID, free_days INTEGER, free_usd NUMERIC)
RETURNS TABLE (state TEXT, why TEXT, free_started_at TIMESTAMPTZ, free_ends_at TIMESTAMPTZ,
               paid_by TEXT, gift_until DATE, free_spent_usd NUMERIC, paddle_status TEXT,
               period_ends_at TIMESTAMPTZ, plan_ends_at TIMESTAMPTZ, first_paid_at TIMESTAMPTZ,
               first_month_spent_usd NUMERIC)
LANGUAGE sql
STABLE
STRICT
SET search_path = pg_catalog, public, pg_temp
AS $$
  WITH found AS (
    SELECT kept.free_started_at, kept.gift_until, kept.paddle_status, kept.period_ends_at,
           kept.ends_at, kept.first_paid_at,
           kept.gift_until >= (now() AT TIME ZONE 'UTC')::date AS gifted,
           kept.paddle_subscription_id IS NOT NULL AS paddled,
           kept.paddle_status IN ('active', 'trialing', 'past_due')
             AND (kept.ends_at IS NULL OR now() < kept.ends_at) AS paddle_live
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
              WHEN found.paddled THEN 'ended'
              WHEN plan.free_started_at IS NULL THEN 'new'
              WHEN now() >= plan.ends_at OR spent.usd >= reader_plan_state.free_usd THEN 'ended'
              ELSE 'free' END,
         CASE WHEN found.gifted OR found.paddle_live THEN NULL
              WHEN found.paddled THEN 'plan'
              WHEN plan.free_started_at IS NULL THEN NULL
              WHEN now() >= plan.ends_at THEN 'time'
              WHEN spent.usd >= reader_plan_state.free_usd THEN 'ceiling' END,
         plan.free_started_at,
         plan.ends_at,
         CASE WHEN found.gifted THEN 'gift' WHEN found.paddle_live THEN 'paddle' END,
         found.gift_until,
         spent.usd,
         found.paddle_status,
         found.period_ends_at,
         found.ends_at,
         found.first_paid_at,
         first_spent.usd
  FROM spent LEFT JOIN plan ON true LEFT JOIN found ON true LEFT JOIN first_spent ON true;
$$;

REVOKE ALL ON FUNCTION reader_plan_state(UUID, INTEGER, NUMERIC) FROM PUBLIC;
