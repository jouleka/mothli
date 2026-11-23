-- ADR 0045 step 4: subscribing through Paddle (app/hosted/subscribe.py, app/hosted/billing.py).
--
-- A checkout the server made for a reader. When a signed-in reader asks to subscribe, the server
-- creates the Paddle transaction itself and puts a random checkout token in its custom data beside
-- the reader's id; Paddle copies that custom data to the subscription the payment makes. A
-- subscription Mothli has not stored becomes the named reader's only when it carries a token kept
-- here for that same reader, unused and unexpired; the first time it is applied the token is used.
-- Only a digest of the token is kept. Anyone holding Paddle's public client token can open a
-- checkout naming any reader, but cannot know a token the server made for that reader. Row-level
-- security forced: the billing role reads and writes the rows of the one account its transaction
-- is set to; nothing else may touch the table (the purge takes an erased account's rows with
-- every other table that has an owner_id).
CREATE TABLE paddle_checkouts (
  owner_id      UUID NOT NULL REFERENCES users(id),
  token_digest  BYTEA NOT NULL CHECK (octet_length(token_digest) = 32),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  used_at       TIMESTAMPTZ,
  PRIMARY KEY (owner_id, token_digest),
  CHECK (expires_at > created_at),
  CHECK (used_at IS NULL OR used_at >= created_at)
);

ALTER TABLE paddle_checkouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE paddle_checkouts FORCE ROW LEVEL SECURITY;
CREATE POLICY paddle_checkouts_owner_isolation ON paddle_checkouts
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

-- Which of Mothli's prices the reader's plan is on, so the account sheet can say monthly or
-- yearly: one of the ids in PADDLE_PRICE_MONTHLY or PADDLE_PRICE_YEARLY, written with the rest of
-- the plan by the billing role. NULL for a plan applied before this column existed until its next
-- message or the regular check brings it.
ALTER TABLE reader_plans
  ADD COLUMN paddle_price_id TEXT CHECK (paddle_price_id ~ '^pri_[a-z0-9]{26}$');

-- The regular check against Paddle (python -m app.hosted.paddle_check): up to ``size`` stored
-- subscriptions of accounts that are not erased, each live or changed in the last 30 days, in a
-- random order so that every one is reached over the runs. It answers stored ids only: the check
-- fetches nothing from Paddle but those. It runs as the migration owner, because the billing role
-- sees only the one account its transaction is set to; only the billing role may execute it
-- (scripts/hosted_roles.py).
CREATE FUNCTION paddle_plans_to_check(size INTEGER)
RETURNS TABLE (reader UUID, subscription TEXT)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT plan.owner_id, plan.paddle_subscription_id
  FROM public.reader_plans AS plan
  JOIN public.users AS person ON person.id = plan.owner_id
  WHERE plan.paddle_subscription_id IS NOT NULL
    AND person.status <> 'deleted'
    AND (plan.paddle_status IN ('active', 'trialing', 'past_due')
         OR plan.paddle_occurred_at > now() - interval '30 days')
  ORDER BY random()
  LIMIT LEAST(GREATEST(paddle_plans_to_check.size, 0), 1000);
$$;

REVOKE ALL ON FUNCTION paddle_plans_to_check(INTEGER) FROM PUBLIC;
