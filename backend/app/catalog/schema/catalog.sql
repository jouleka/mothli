-- LIT-18 — global catalog database (catalog.db). Lifted near-verbatim from the spike
-- (spikes/lit-5-schema/catalog.sql) per ADR 0007 D-A1. All DDL is IF NOT EXISTS (idempotent on open).
-- =============================================================================
-- ONE small global DB for the whole app: the shelf, the MUTABLE reading state (bookmark/CFI/ingest),
-- and the cost ledger. The per-book memory.db files are IMMUTABLE ground truth and deliberately do NOT
-- store "the current bookmark" — the bookmark is reading state, passed INTO the DAL as an argument.
-- This separation makes time-travel trivial (pass a different integer) and keeps memory.db append-only.
-- =============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS books (
  book_id           TEXT PRIMARY KEY,     -- same id stamped inside that book's memory.db
  title             TEXT NOT NULL,
  author            TEXT,
  source            TEXT,                 -- gutenberg | standard-ebooks | upload
  source_id         TEXT,
  file_hash         TEXT,                 -- sha256 of the imported .epub (dedupe / re-import detect)
  cover_path        TEXT,
  db_path           TEXT NOT NULL,        -- e.g. books/<book_id>/memory.db
  schema_version    INTEGER NOT NULL,
  incarnation       TEXT NOT NULL,        -- unique shelf lifetime; changes on delete/re-import
  added_at          TEXT NOT NULL,
  last_opened_at    TEXT                  -- reserved for the LIT route layer (no setter yet)
);

-- Per-book reading position. bookmark = the spoiler frontier the DAL consumes — the highest FULLY-READ
-- chapter ordinal (a MONOTONIC high-water mark; LIT-12 maps the continuous reader CFI onto it).
CREATE TABLE IF NOT EXISTS reading_state (
  book_id           TEXT PRIMARY KEY REFERENCES books(book_id),
  bookmark          INTEGER NOT NULL DEFAULT 0,
  cfi               TEXT,                 -- precise reader position (LIT-12/LIT-13)
  ingest_progress   INTEGER NOT NULL DEFAULT 0,  -- highest chapter ordinal ingested (monotonic)
  position_epoch    INTEGER NOT NULL DEFAULT 0,  -- LIT-17: increments on an explicit new reading pass
  updated_at        TEXT
);

-- Exact primary-text boundary for conversation, separate from chapter-stamped story memory.
CREATE TABLE IF NOT EXISTS conversation_frontier (
  book_id TEXT PRIMARY KEY REFERENCES reading_state(book_id) ON DELETE CASCADE,
  position_epoch INTEGER NOT NULL,
  atom INTEGER NOT NULL,
  source_offset INTEGER NOT NULL,
  source_hash TEXT NOT NULL,
  cfi TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Per-book spend (cost-to-date; feeds LIT-21 ceilings).
CREATE TABLE IF NOT EXISTS cost_ledger (
  entry_id          INTEGER PRIMARY KEY,
  book_id           TEXT NOT NULL REFERENCES books(book_id),
  chapter_ordinal   INTEGER,
  phase             TEXT,                 -- extraction | synthesis | embedding
  model             TEXT,
  input_tokens      INTEGER,
  output_tokens     INTEGER,
  usd               REAL,
  at                TEXT
);

CREATE INDEX IF NOT EXISTS ix_cost_book ON cost_ledger(book_id);

-- Pre-call reservations make ceilings race-safe. A process crash intentionally leaves the
-- reservation behind: unknown provider spend must continue to count until an operator reconciles it.
CREATE TABLE IF NOT EXISTS cost_reservations (
  reservation_id       TEXT PRIMARY KEY,
  book_id               TEXT NOT NULL REFERENCES books(book_id),
  chapter_ordinal       INTEGER,
  phase                 TEXT NOT NULL,
  model                 TEXT,
  reserved_input_tokens INTEGER NOT NULL,
  reserved_output_tokens INTEGER NOT NULL,
  reserved_usd          REAL NOT NULL,
  actual_input_tokens   INTEGER,
  actual_output_tokens  INTEGER,
  actual_usd            REAL,
  at                    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cost_reservation_book ON cost_reservations(book_id);

-- Mothli's checked notes on one chapter. A function of the chapter's own text, so every reading
-- pass shares them; a reply may use them only when its read prefix of that chapter hashes to
-- source_hash, which is true only when the whole, unchanged chapter is inside the read position.
CREATE TABLE IF NOT EXISTS chapter_notes (
  book_id     TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
  atom        INTEGER NOT NULL,
  source_hash TEXT NOT NULL,             -- of the normalized chapter text the notes were written from
  version     TEXT NOT NULL,             -- prompt and check version; a change rebuilds
  sentences   TEXT NOT NULL,             -- JSON array of sentences that passed every check
  dropped     INTEGER NOT NULL,          -- sentences the checks rejected
  created_at  TEXT NOT NULL,
  PRIMARY KEY (book_id, atom)
);

-- One row per message. drew_on keeps what the turn was written from, as JSON
-- {book_id: [incarnation, {atom: [[start, end]]}]}: the read map of its book for a turn inside a
-- book, and of every book that had anything read for a conversation across books (book_id NULL).
-- A turn is read back only while all of that is still read. drew_on is NULL on a turn stored
-- before it was kept, and such a turn is never read back. (atom, source_offset) is where the
-- reader was when they asked; it decides nothing.
CREATE TABLE IF NOT EXISTS conversation_turns (
  turn_id        INTEGER PRIMARY KEY,
  book_id        TEXT REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation    TEXT,                   -- shelf lifetime; a re-import does not inherit turns
  position_epoch INTEGER,
  atom           INTEGER,
  source_offset  INTEGER,
  role           TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content        TEXT NOT NULL,          -- the reader's words, or only the sentences shown
  reaction       INTEGER NOT NULL DEFAULT 0,  -- a reader turn judged a reaction to the book
  request_id     TEXT,
  created_at     TEXT NOT NULL,
  drew_on        TEXT
);

CREATE INDEX IF NOT EXISTS ix_turns_book ON conversation_turns(book_id, atom, source_offset, turn_id);

-- The read map: the stretches of each chapter the reader has read, and how that is known.
-- Only text inside it may reach a model. Offsets are in the chapter's normalized text.
CREATE TABLE IF NOT EXISTS read_ranges (
  book_id      TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation  TEXT NOT NULL,
  atom         INTEGER NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset   INTEGER NOT NULL,          -- exclusive
  source       TEXT NOT NULL CHECK (source IN ('read','told','import')),
  updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_read_ranges_book ON read_ranges(book_id, incarnation, atom);

-- Pages turned quickly, one after another: seen, not read. Kept apart from read_ranges so that
-- nothing on them can reach a model; used only to ask the reader whether they read those chapters.
CREATE TABLE IF NOT EXISTS skimmed_ranges (
  book_id      TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation  TEXT NOT NULL,
  atom         INTEGER NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset   INTEGER NOT NULL,          -- exclusive
  updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_skimmed_ranges_book ON skimmed_ranges(book_id, incarnation, atom);

-- Where the reader is now. Unlike the read map it may move backward.
CREATE TABLE IF NOT EXISTS read_position (
  book_id       TEXT PRIMARY KEY REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation   TEXT NOT NULL,
  atom          INTEGER NOT NULL,
  source_offset INTEGER NOT NULL,
  updated_at    TEXT NOT NULL
);

-- What Mothli remembers about the reader between conversations: one short note a row, distilled
-- from the reader's own messages and checked against the message it came from (app/recollections.py).
-- Not the per-book memory.db and not chapter_notes: those are about books, this is about the reader.
-- drew_on is the record the source message was stored with (see conversation_turns). A tied note
-- says something of a book and is used and listed only while all of drew_on is still read; for
-- any note, `said` is shown only while it is. What the reader has read is never stored here.
CREATE TABLE IF NOT EXISTS recollections (
  recollection_id INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('about','taste','plan','feeling')),
  text        TEXT NOT NULL,             -- one sentence about the reader, third person
  tied        INTEGER NOT NULL,          -- 1: says something of a book; follows what is read of it
  book_id     TEXT REFERENCES books(book_id) ON DELETE CASCADE,  -- the book it is tied to, if one
  drew_on     TEXT,                      -- what was read when the reader said it
  said        TEXT NOT NULL,             -- the words of the message it came from, kept here because
                                         -- the conversation may be cleared and turn ids are reused
  said_at     TEXT NOT NULL,             -- when the reader said it
  created_at  TEXT NOT NULL,
  used_at     TEXT NOT NULL              -- last given to a reply; the least recently used go first
);

-- The last message of each conversation that has been looked at for recollections, so that none
-- is distilled twice and a deleted note is not written again from the same messages.
CREATE TABLE IF NOT EXISTS recollection_cursors (
  conversation TEXT NOT NULL,            -- book_id, or '' for the conversation across books
  incarnation  TEXT NOT NULL,
  turn_id      INTEGER NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (conversation, incarnation)
);
