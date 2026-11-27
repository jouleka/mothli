-- A refund or a chargeback ends the plan (app/hosted/billing.py, app/hosted/subscribe.py).
--
-- Paddle does not cancel a subscription when one of its transactions is refunded or charged back:
-- it tells of the refund in an adjustment message (adjustment.created, adjustment.updated). When
-- an approved full refund, or a chargeback, of the subscription Mothli stored for a reader
-- arrives, the moment it was applied is kept here, from the database clock, and the plan's
-- scheduled end (ends_at) is set to that moment, so the plan is no longer live everywhere the
-- plan is read (reader_plan_state from 0042 answers 'ended' with why 'plan'; nothing about it
-- changes). It stays set for that subscription: a later message about the same subscription can
-- never move its end past this moment (Paddle may still say 'active' until Mothli's cancel reaches
-- it, and messages arrive in any order). A new subscription of the reader's own, made by a new
-- checkout after this, clears it. NULL otherwise, and on every row already there.
ALTER TABLE reader_plans ADD COLUMN refunded_at TIMESTAMPTZ;

-- The adjustment messages about a stored subscription are recorded with the subscription ones,
-- by their id, once. 'unchanged': one of Mothli's that ends nothing (a partial refund, a refund
-- still waiting for Paddle's approval or rejected, a credit, a plan already ended by a refund).
ALTER TABLE paddle_events DROP CONSTRAINT paddle_events_event_type_check;
ALTER TABLE paddle_events ADD CONSTRAINT paddle_events_event_type_check
  CHECK (event_type ~ '^(subscription|adjustment)\.[a-z_]{1,32}$');
ALTER TABLE paddle_events DROP CONSTRAINT paddle_events_outcome_check;
ALTER TABLE paddle_events ADD CONSTRAINT paddle_events_outcome_check
  CHECK (outcome IN ('applied', 'older', 'other_subscription', 'no_account', 'unchanged'));
