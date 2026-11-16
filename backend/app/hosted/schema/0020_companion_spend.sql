-- ADR 0041 part 3: the platform pays for the companion, with caps per reader per UTC day and
-- month, a cap on a reader's companion calls in flight, and a cap on the platform's own UTC day.
ALTER TABLE owner_limits
  ADD COLUMN max_spend_usd_per_day   NUMERIC(20,10)
    CHECK (max_spend_usd_per_day IS NULL OR max_spend_usd_per_day >= 0),
  ADD COLUMN max_spend_usd_per_month NUMERIC(20,10)
    CHECK (max_spend_usd_per_month IS NULL OR max_spend_usd_per_month >= 0),
  ADD COLUMN max_companion_calls_in_flight INTEGER NOT NULL DEFAULT 8
    CHECK (max_companion_calls_in_flight > 0);

-- The companion's own name for a call (stream-reply, stream-sentence-check, notes-write, ...);
-- NULL for the older reader-key paths. Set on a row exactly when the companion wrote it.
ALTER TABLE cost_ledger
  ADD COLUMN purpose TEXT CHECK (purpose IS NULL OR purpose ~ '^[a-z][a-z0-9-]{0,63}$');
ALTER TABLE cost_reservations
  ADD COLUMN purpose TEXT CHECK (purpose IS NULL OR purpose ~ '^[a-z][a-z0-9-]{0,63}$');

CREATE INDEX ix_cost_ledger_owner_companion_time
  ON cost_ledger (owner_id, recorded_at) WHERE purpose IS NOT NULL;

-- Service-scoped, content-free: the platform's own spend per UTC day. No owner, no book, no
-- model: one sum per day. The tenant role holds no privilege on it and cannot read it; it may
-- only call the two functions below, which answer "fits" or nothing, and never a sum.
CREATE TABLE platform_spend_days (
  day           DATE PRIMARY KEY,
  settled_usd   NUMERIC(20,10) NOT NULL DEFAULT 0 CHECK (settled_usd >= 0),
  reserved_usd  NUMERIC(20,10) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
REVOKE ALL ON platform_spend_days FROM PUBLIC;

-- Reserve ``amount`` against today's platform total when it fits under ``day_cap``. The UPDATE
-- takes the day's row lock and, at the default isolation level, checks its condition again on the
-- row a concurrent reservation left, so two readers cannot both pass the last cent. A negative or
-- NULL argument reserves nothing and answers false or NULL: the caller refuses either.
CREATE FUNCTION companion_platform_reserve(amount NUMERIC, day_cap NUMERIC)
RETURNS BOOLEAN
LANGUAGE sql
STRICT
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  INSERT INTO public.platform_spend_days (day)
  VALUES ((now() AT TIME ZONE 'UTC')::date) ON CONFLICT (day) DO NOTHING;
  WITH taken AS (
    UPDATE public.platform_spend_days
       SET reserved_usd = reserved_usd + amount, updated_at = now()
     WHERE day = (now() AT TIME ZONE 'UTC')::date
       AND amount >= 0 AND day_cap >= 0
       AND settled_usd + reserved_usd + amount <= day_cap
    RETURNING 1
  )
  SELECT EXISTS (SELECT 1 FROM taken);
$$;

-- Move one reservation's amount on the day it was made: an estimate replaced by the actual
-- (reserved_change), settled into the ledger (reserved down, settled up) or released (reserved
-- down). Never fails on arithmetic, so a reader's settlement is never lost to it.
CREATE FUNCTION companion_platform_adjust(spend_day DATE, reserved_change NUMERIC,
                                          settled_change NUMERIC)
RETURNS VOID
LANGUAGE sql
STRICT
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
  INSERT INTO public.platform_spend_days (day) VALUES (spend_day) ON CONFLICT (day) DO NOTHING;
  UPDATE public.platform_spend_days
     SET reserved_usd = GREATEST(reserved_usd + reserved_change, 0),
         settled_usd = settled_usd + GREATEST(settled_change, 0),
         updated_at = now()
   WHERE day = spend_day;
$$;

REVOKE ALL ON FUNCTION companion_platform_reserve(NUMERIC, NUMERIC) FROM PUBLIC;
REVOKE ALL ON FUNCTION companion_platform_adjust(DATE, NUMERIC, NUMERIC) FROM PUBLIC;
