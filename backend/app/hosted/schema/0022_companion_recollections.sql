-- ADR 0041 part 7: what Mothli remembers about a reader between conversations, per owner. A note is
-- distilled from the reader's own messages by a durable recollect job; app/recollections.py decides
-- what is kept and when a note may be used. recollection_id orders the notes and numbers them for
-- the model; public_id is the only id a URL carries.
CREATE TABLE recollections (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recollection_id   BIGINT GENERATED ALWAYS AS IDENTITY,
  public_id         UUID NOT NULL DEFAULT gen_random_uuid(),
  kind              TEXT NOT NULL CHECK (kind IN ('about', 'taste', 'plan', 'feeling')),
  text              TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 1000),
  tied              BOOLEAN NOT NULL,
  book_id           UUID,
  book_incarnation  UUID,
  drew_on           JSONB CHECK (drew_on IS NULL OR jsonb_typeof(drew_on) = 'object'),
  said              TEXT NOT NULL CHECK (length(said) <= 2000),
  said_at           TIMESTAMPTZ NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  used_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, recollection_id),
  UNIQUE (owner_id, public_id),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE,
  CHECK ((book_id IS NULL) = (book_incarnation IS NULL))
);
CREATE INDEX ix_recollections_owner_used ON recollections (owner_id, used_at, recollection_id);
ALTER TABLE recollections ENABLE ROW LEVEL SECURITY;
ALTER TABLE recollections FORCE ROW LEVEL SECURITY;
CREATE POLICY recollections_owner_isolation ON recollections
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

-- How far each conversation of an owner has been looked at: the id of the last turn read. '' is
-- the home conversation (incarnation 'home'), as locally. A batch is stored only if the cursor
-- still holds the value read with it (a compare-and-set), so two workers never store one twice.
CREATE TABLE recollection_cursors (
  owner_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation  TEXT NOT NULL CHECK (conversation = '' OR conversation ~ '^[0-9a-f-]{36}$'),
  incarnation   TEXT NOT NULL CHECK (incarnation = 'home' OR incarnation ~ '^[0-9a-f-]{36}$'),
  turn_id       BIGINT NOT NULL CHECK (turn_id >= 0),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, conversation, incarnation),
  CHECK ((conversation = '') = (incarnation = 'home'))
);
ALTER TABLE recollection_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE recollection_cursors FORCE ROW LEVEL SECURITY;
CREATE POLICY recollection_cursors_owner_isolation ON recollection_cursors
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
