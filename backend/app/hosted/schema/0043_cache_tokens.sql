-- Recording only: how a call's input tokens were billed. Of a call's input_tokens (the provider's
-- total, which already includes the cached part), cache_read_tokens were read from the provider's
-- prompt cache and cache_write_tokens were written to it; the rest were billed at full price. A
-- reservation keeps the two numbers once the call's usage is known, and its settlement copies them
-- into the ledger. Rows from before this, and calls whose split is not known (a failed call, an
-- embedding, a Jev check), stay NULL. Nothing reads them to decide or charge anything: the money of
-- each call is computed and recorded exactly as before. The existing table grants cover the new
-- columns, so no role's privileges change.
ALTER TABLE cost_ledger
  ADD COLUMN cache_read_tokens BIGINT CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
  ADD COLUMN cache_write_tokens BIGINT CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0);

ALTER TABLE cost_reservations
  ADD COLUMN cache_read_tokens BIGINT CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
  ADD COLUMN cache_write_tokens BIGINT CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0);
