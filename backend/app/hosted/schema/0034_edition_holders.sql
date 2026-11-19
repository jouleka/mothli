-- ADR 0044 step 8: erasing an edition nobody holds. The daily purge marks an edition (a books
-- row of the notes account, marked by edition_of) with the time it first found no reader holding
-- its file, clears the mark when a holder is back, and erases the edition once the mark is 30
-- days old. The mark is a time and nothing else: it names no reader, no reader's book and no
-- file. The purge's new privilege (UPDATE on books) is in app/hosted/tenant/grants.py, as every
-- role's is.

-- "No holder since". Only a live edition can carry it: a reader's book, an unmarked book of the
-- notes account and an erased edition never do. Every row starts without it, so no edition's
-- 30 days are counted from the deployment: the first purge run after it starts them.
ALTER TABLE books ADD COLUMN no_holder_since TIMESTAMPTZ;
ALTER TABLE books
  ADD CONSTRAINT books_no_holder_since_ck
    CHECK (no_holder_since IS NULL OR (edition_of IS NOT NULL AND deleted_at IS NULL));
