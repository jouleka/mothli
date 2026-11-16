-- ADR 0041 part 2: what was said in a conversation, per owner; a book's or the home conversation.
-- turn_id is the order of a conversation and never appears in a URL or a response.
CREATE TABLE conversation_turns (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  turn_id           BIGINT GENERATED ALWAYS AS IDENTITY,
  book_id           UUID,
  book_incarnation  UUID,
  position_epoch    BIGINT CHECK (position_epoch >= 0),
  atom              INTEGER CHECK (atom >= 0),
  source_offset     INTEGER CHECK (source_offset >= 0),
  role              TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content           TEXT NOT NULL CHECK (length(content) <= 20000),
  reaction          BOOLEAN NOT NULL DEFAULT false,
  request_id        TEXT CHECK (request_id IS NULL OR request_id ~ '^[0-9a-f]{12}$'),
  drew_on           JSONB CHECK (drew_on IS NULL OR jsonb_typeof(drew_on) = 'object'),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, turn_id),
  FOREIGN KEY (owner_id, book_id, book_incarnation)
    REFERENCES books(owner_id, id, incarnation) ON DELETE CASCADE,
  CHECK ((book_id IS NULL) = (book_incarnation IS NULL))
);
CREATE INDEX ix_conversation_turns_owner_conversation
  ON conversation_turns (owner_id, book_id, book_incarnation, turn_id);
ALTER TABLE conversation_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_turns FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_turns_owner_isolation ON conversation_turns
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
