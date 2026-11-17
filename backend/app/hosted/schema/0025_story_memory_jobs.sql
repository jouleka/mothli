-- ADR 0042 part 3: story memory is built by a durable companion job, story_memory, one chapter of
-- one book per run, on the existing job table. Like chapter notes it names its book and no
-- credential (the platform pays), and at most one waits for an owner and book.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_ck;
ALTER TABLE jobs DROP CONSTRAINT jobs_companion_shape_ck;
ALTER TABLE jobs
  ADD CONSTRAINT jobs_kind_ck
    CHECK (kind IN ('ingest_book', 'chapter_notes', 'recollect', 'story_memory')),
  ADD CONSTRAINT jobs_companion_shape_ck
    CHECK (kind = 'ingest_book' OR (
      credential_id IS NULL
      AND (kind NOT IN ('chapter_notes', 'story_memory') OR book_id IS NOT NULL)
    ));

DROP INDEX ux_jobs_one_pending_companion_job;
CREATE UNIQUE INDEX ux_jobs_one_pending_companion_job
  ON jobs (owner_id, kind, COALESCE(book_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE state = 'pending' AND kind IN ('chapter_notes', 'recollect', 'story_memory');

-- A chapter whose extraction the review rejected after its one repair. It is not retried within
-- the job; two such failed runs for the same chapter (named in the job's payload_metadata) make
-- the next run keep it as an unremembered chapter (ADR 0042 section 2).
ALTER TABLE jobs DROP CONSTRAINT jobs_failure_shape_ck;
ALTER TABLE jobs
  ADD CONSTRAINT jobs_failure_shape_ck
    CHECK (
      sanitized_error IS NULL OR (
        sanitized_error ? 'code'
        AND sanitized_error ? 'retryable'
        AND jsonb_typeof(sanitized_error->'code') = 'string'
        AND sanitized_error->>'code' IN (
          'attempts_exhausted','budget_exceeded','cancelled','grounding_rejected',
          'internal_error','invalid_model_output','provider_rejected','provider_unavailable',
          'source_integrity','source_missing'
        )
        AND jsonb_typeof(sanitized_error->'retryable') = 'boolean'
        AND sanitized_error - ARRAY['code', 'retryable'] = '{}'::jsonb
      )
    );

ALTER TABLE job_attempts DROP CONSTRAINT job_attempts_failure_shape_ck;
ALTER TABLE job_attempts
  ADD CONSTRAINT job_attempts_failure_shape_ck
    CHECK (
      sanitized_error IS NULL OR (
        sanitized_error ? 'code'
        AND sanitized_error ? 'retryable'
        AND jsonb_typeof(sanitized_error->'code') = 'string'
        AND sanitized_error->>'code' IN (
          'attempts_exhausted','budget_exceeded','cancelled','grounding_rejected',
          'internal_error','invalid_model_output','provider_rejected','provider_unavailable',
          'source_integrity','source_missing'
        )
        AND jsonb_typeof(sanitized_error->'retryable') = 'boolean'
        AND sanitized_error - ARRAY['code', 'retryable'] = '{}'::jsonb
      )
    );

-- A chapter kept as unremembered carries kind 'unremembered' (every other chapter keeps the
-- default 'body'); nothing else in the schema changes for it.
