-- ADR 0045 step 7: deleting an account while paying (app/hosted/auth/account.py,
-- app/hosted/subscribe.py, app/hosted/paddle_check.py, app/hosted/purge.py).
--
-- When the reader asked to delete their account, from the database clock; NULL otherwise. From
-- then on Mothli owes them the cancel of their Paddle plan at the end of the period already paid
-- for, so they are never charged again: whenever their stored plan is live with no scheduled end
-- (paddle_status active, trialing or past_due, and ends_at NULL), the regular check asks Paddle
-- for that cancel until Paddle's answer brings the scheduled end. Coming back within the 30 days
-- leaves it set (the plan still ends); only "Keep my plan" and a new checkout of the reader's own
-- clear it. Written only by the billing role, for the account its transaction is set to; the
-- purge takes it with the rest of the row.
ALTER TABLE reader_plans ADD COLUMN cancel_owed_since TIMESTAMPTZ;

-- The regular check's list, made again with the same signature and rights: the plans whose
-- cancel is owed come first, so the check reaches them in its next run however many plans there
-- are; the rest as before, in a random order.
CREATE OR REPLACE FUNCTION paddle_plans_to_check(size INTEGER)
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
  ORDER BY (plan.cancel_owed_since IS NOT NULL
            AND plan.paddle_status IN ('active', 'trialing', 'past_due')
            AND plan.ends_at IS NULL) DESC,
           random()
  LIMIT LEAST(GREATEST(paddle_plans_to_check.size, 0), 1000);
$$;

REVOKE ALL ON FUNCTION paddle_plans_to_check(INTEGER) FROM PUBLIC;
