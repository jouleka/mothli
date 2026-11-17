-- ADR 0041 part 8: a reader's account is kept for 30 days after they ask to delete it, then erased.
--   active:   status 'active',   erase_after NULL
--   pending:  status 'deleting', erase_after set (the reader may sign in only to keep it or leave)
--   erasing:  status 'deleted',  erase_after set, deleted_at set (files and rows still going)
--   erased:   status 'deleted',  erase_after NULL, deleted_at set (nothing left but this id)
ALTER TABLE users
  ADD COLUMN erase_after TIMESTAMPTZ,
  ADD CONSTRAINT ck_users_erasure_state CHECK (
    CASE status
      WHEN 'deleting' THEN erase_after IS NOT NULL AND deleted_at IS NULL
      WHEN 'deleted' THEN deleted_at IS NOT NULL
      ELSE erase_after IS NULL
    END
  );

CREATE INDEX ix_users_erase_after ON users (erase_after) WHERE erase_after IS NOT NULL;
