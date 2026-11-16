-- ADR 0041 part 6: Mothli's notes on finished chapters, per owner and book incarnation, written by
-- durable companion jobs on the existing job table. A note is usable only while the chapter text
-- inside the reader's read position hashes to source_hash (app/chapter_notes.py decides that).
CREATE TABLE chapter_notes (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  atom              INTEGER NOT NULL CHECK (atom > 0),
  source_hash       TEXT NOT NULL CHECK (source_hash ~ '^[0-9a-f]{16}$'),
  version           TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
  sentences         JSONB NOT NULL CHECK (jsonb_typeof(sentences) = 'array'),
  dropped           INTEGER NOT NULL CHECK (dropped >= 0),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation, atom),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);
ALTER TABLE chapter_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_notes FORCE ROW LEVEL SECURITY;
CREATE POLICY chapter_notes_owner_isolation ON chapter_notes
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

-- The companion's job kinds beside extraction. A companion job names no credential (the platform
-- pays) and chapter notes always name their book; recollect is part 7's.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_ck;
ALTER TABLE jobs
  ADD CONSTRAINT jobs_kind_ck
    CHECK (kind IN ('ingest_book', 'chapter_notes', 'recollect')),
  ADD CONSTRAINT jobs_companion_shape_ck
    CHECK (kind = 'ingest_book' OR (
      credential_id IS NULL AND (kind <> 'chapter_notes' OR book_id IS NOT NULL)
    ));

-- At most one companion job of an owner, kind and book waits; asking again while it waits adds
-- nothing (INSERT ... ON CONFLICT DO NOTHING). The claim lets at most one of them run.
CREATE UNIQUE INDEX ux_jobs_one_pending_companion_job
  ON jobs (owner_id, kind, COALESCE(book_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE state = 'pending' AND kind IN ('chapter_notes', 'recollect');

-- Service-scoped and content-free: the platform's provider account was refused (no credits, or a
-- key it does not accept), so companion jobs wait until paused_until for every reader. Written and
-- read by the worker role only.
CREATE TABLE provider_pauses (
  provider      TEXT PRIMARY KEY CHECK (provider IN ('typesafe', 'openai-compatible', 'anthropic')),
  reason        TEXT NOT NULL CHECK (reason IN ('no_credits', 'unauthorized')),
  paused_until  TIMESTAMPTZ NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
REVOKE ALL ON provider_pauses FROM PUBLIC;
