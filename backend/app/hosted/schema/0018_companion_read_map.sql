-- ADR 0041 part 1: what the reader has read, per owner and book incarnation.
CREATE TABLE read_ranges (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  atom              INTEGER NOT NULL CHECK (atom > 0),
  start_offset      INTEGER NOT NULL CHECK (start_offset >= 0),
  end_offset        INTEGER NOT NULL CHECK (end_offset > start_offset),
  source            TEXT NOT NULL CHECK (source IN ('read', 'told', 'import')),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation, atom, source, start_offset),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);
ALTER TABLE read_ranges ENABLE ROW LEVEL SECURITY;
ALTER TABLE read_ranges FORCE ROW LEVEL SECURITY;
CREATE POLICY read_ranges_owner_isolation ON read_ranges
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

-- Pages turned through quickly: seen in passing, never part of the read map.
CREATE TABLE skimmed_ranges (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  atom              INTEGER NOT NULL CHECK (atom > 0),
  start_offset      INTEGER NOT NULL CHECK (start_offset >= 0),
  end_offset        INTEGER NOT NULL CHECK (end_offset > start_offset),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation, atom, start_offset),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);
ALTER TABLE skimmed_ranges ENABLE ROW LEVEL SECURITY;
ALTER TABLE skimmed_ranges FORCE ROW LEVEL SECURITY;
CREATE POLICY skimmed_ranges_owner_isolation ON skimmed_ranges
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

-- Where the reader is in the read map; its existence marks the map as begun for that incarnation.
CREATE TABLE read_position (
  owner_id          UUID NOT NULL,
  book_id           UUID NOT NULL,
  book_incarnation  UUID NOT NULL,
  atom              INTEGER NOT NULL CHECK (atom >= 0),
  source_offset     INTEGER NOT NULL CHECK (source_offset >= 0),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, book_id, book_incarnation),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE
);
ALTER TABLE read_position ENABLE ROW LEVEL SECURITY;
ALTER TABLE read_position FORCE ROW LEVEL SECURITY;
CREATE POLICY read_position_owner_isolation ON read_position
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
