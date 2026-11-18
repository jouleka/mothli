-- Mothli is one hosted product (ADR 0043, step 7): the reader-key extraction job, ingest_book, is
-- retired. No handler for it was ever written, uploads stopped making it in step 6, and its routes,
-- its "jobs under way" count and the worker's key lookup for it are gone. Story memory jobs build
-- what it was meant to build (ADR 0042). The column jobs.credential_id stays until step 8.

-- Jobs of the retired kind are deleted, with their attempts (job_attempts cascades); the audit
-- trail keeps their content-free lines. Nothing could run one, so none should have a cost
-- reservation or a ledger line. If one has, those rows reference the job without cascading, the
-- delete fails and the whole migration stops: what was spent is never lost, and is settled by hand.
DELETE FROM jobs WHERE kind = 'ingest_book';

-- The kind, and the chapter count only it carried.
ALTER TABLE jobs DROP CONSTRAINT jobs_ingest_chapter_count_ck;
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_ck;
ALTER TABLE jobs DROP CONSTRAINT jobs_companion_shape_ck;
ALTER TABLE jobs
  ADD CONSTRAINT jobs_kind_ck
    CHECK (kind IN ('chapter_notes', 'recollect', 'story_memory')),
  -- The same rule as before for every remaining kind, in two parts, so that dropping
  -- credential_id in step 8 takes only the second with it.
  ADD CONSTRAINT jobs_companion_shape_ck
    CHECK (kind NOT IN ('chapter_notes', 'story_memory') OR book_id IS NOT NULL),
  ADD CONSTRAINT jobs_names_no_credential_ck
    CHECK (credential_id IS NULL);

-- The state only that kind was ever in: waiting for a reader's key. A job of another kind left in
-- it (none can be: nothing else ever wrote it) makes the new check fail and the migration stop.
ALTER TABLE jobs DROP CONSTRAINT jobs_state_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_state_check
  CHECK (state IN ('pending', 'leased', 'running', 'succeeded', 'failed', 'cancelled'));

-- The reader's limit on jobs under way, which counted only that kind, and the index that served
-- the count.
DROP INDEX ix_jobs_owner_active_limit;
ALTER TABLE owner_limits DROP COLUMN max_active_jobs;
