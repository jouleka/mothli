# Mothli as a reader-level companion: plan and data model

15 November 2026. Starts from PR #46. Stages 0 to 3 are built; what was built and measured is in section 6m of `companion-audit-2026-11-14.md`. Two things differ from the plan below: a reply uses a chapter's notes when its read prefix of that chapter hashes to the text the notes were written from (no position comparison), and notes sit behind `READER_STORY_NOTES`, which follows streamed replies when unset and so is on wherever they are (audit section 6q).

## What the code does today (read, not run)

- Streamed replies exist only in local mode (`api/views.py:570`). Hosted mode has its own `/ask` and no stream path. `codex-local` is rejected outside local mode (`config.py:165`).
- Local mode has one reader and no reader id. State is in `catalog.db` (books, `reading_state`, `conversation_frontier`), one `memory.db` per book, and `reading-history.db`.
- Conversation history lives in the browser (`localStorage`, 40 exchanges); the browser sends the last 8 messages with each question.
- No reader reaction is stored anywhere. `reader_reactions.py` is a table of canned replies.
- A library book has no stored "finished" state. Imported history entries have status, rating, review and highlights.
- Only classics have a blurb (Project Gutenberg's summary field). Uploaded EPUBs have none; `dc:description` is not parsed.
- `codex-local` returns the whole reply at once, so its timings say nothing about time to first words.

## Plan

Local mode first. Tables are shaped so the hosted port is the same columns plus `owner_id`.

| Stage | What | Model calls added |
|---|---|---|
| 0 | Eval: positions inside a chapter, whole-book cases, cross-book cases (written now, run in stage 4). Baseline run of the current path on the new cases | none in the product |
| 1 | Story so far: checked notes per finished chapter, given to the writer and the checker | per chapter, once: 1 writer call, 1 Jev check per note sentence |
| 2 | Conversation turns on the server, stamped with the exact position. A reaction is a flag on a reader turn | 0 |
| 3 | Reader profile, computed on request from books, positions, history and flagged turns | 0 |
| 4 | Homepage chat. Not started until 1–3 are measured | later |

### 1. Story so far

- When the reading position passes the end of a chapter, one writer call turns that chapter's text into three to six one-fact sentences. Earlier notes are given only so "he" has a name; they are not evidence.
- Each note sentence goes through the same gate as a reply sentence (`gate_sentences`: Jev check, one second look, unread-name and foreshadowing checks) against that chapter's own passages. A sentence that fails is dropped. Only checked sentences are stored.
- A question uses the notes of chapters wholly inside the read position. Selection is by position at request time, so "start over" hides later notes without deleting them.
- The chapter in progress gets no note. The writer already receives its last four passages. The mid-chapter eval cases show whether that is enough.
- Built in the background as the reader advances; a question never waits for it and works as today when notes are missing.

**This changes what a reply sentence is checked against. Three ways:**

| | Checker sees | Guarantee | Cost |
|---|---|---|---|
| A. Writer only | The ten passages, as today | Unchanged | Whole-book answers are mostly dropped: ten passages cannot prove a summary of thirty chapters |
| **B. Notes as evidence (my pick)** | Ten passages plus the notes | Position boundary unchanged: every input is read text. Looser on faithfulness: a sentence may rest on a note that was itself checked against the pages, not on the pages directly | Smallest change |
| C. Notes as an index | Ten passages plus the primary passages each relevant note was checked against | Unchanged: primary text only | More passages per check (untested above ten), more code |

I would build B, then audit every shown sentence that only the notes support against the chapter text, as in audit section 6k. If that audit finds unsupported sentences, switch to C.

**Not fixed by this work:** a forward jump still marks skipped chapters as read (audit problem 9), so notes would be built for chapters the reader skipped.

### 2. Conversations and reactions

- The server stores each reader message and the sentences it actually showed, with the position they were written at.
- For a question the server loads the last eight turns at or before the request position itself.
- A reader message the turn classifier already marks as a reaction (`book_reaction` ≥ 0.7, computed today and thrown away) is flagged. No extra call, no new UI.
- Removed: the `history` field on the stream request, and the browser as the source of history. The panel loads turns from the server.

### 3. Reader profile

Computed, not stored: for each library book its status (unread, in progress with a fraction, finished), last opened, and the reader's latest flagged reactions at or before the position; plus imported history entries (status, rating, finished date). No model call and no table. A model-written "taste" line would be an unchecked claim about the reader, so it waits for a measured need.

### 4. Homepage chat (design only)

- Same stream gate. Evidence per book: notes up to that book's position, passages from that book's read pages, each labelled with its book. Unread books: blurb only.
- Suggestions come only from the classics catalog, with the blurb as the evidence a sentence is checked against. The writer's own knowledge of books is never a source.
- Two risks to settle before building:
  - Gutenberg's summaries are not known to be spoiler-free. They need a screening pass, or a cut to the opening sentence.
  - The unread-name check is per book; a name unread in one book can be read in another. Each sentence must be tied to a book before that book's check runs.
- A book finished elsewhere with no text in the library can only be discussed from its blurb and the reader's own review and highlights.

## Data model (catalog.db)

```sql
-- Checked notes for one fully read chapter. A function of the chapter text, so shared by
-- every reading pass; used only when the chapter is wholly inside the read position.
CREATE TABLE IF NOT EXISTS chapter_notes (
  book_id     TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
  atom        INTEGER NOT NULL,          -- chapter ordinal
  source_hash TEXT NOT NULL,             -- content hash of the chapter text the notes were built from
  version     TEXT NOT NULL,             -- prompt and check version; a change rebuilds
  sentences   TEXT NOT NULL,             -- JSON array of checked sentences; may be empty
  dropped     INTEGER NOT NULL,          -- sentences the gate rejected
  created_at  TEXT NOT NULL,
  PRIMARY KEY (book_id, atom)
);

-- One row per message. book_id NULL = homepage chat (stage 4).
CREATE TABLE IF NOT EXISTS conversation_turns (
  turn_id        INTEGER PRIMARY KEY,
  book_id        TEXT REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation    TEXT,                   -- shelf lifetime; a re-import does not inherit turns
  position_epoch INTEGER,
  atom           INTEGER,                -- exact position the turn was written at
  source_offset  INTEGER,
  role           TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content        TEXT NOT NULL,          -- reader's words, or only the sentences shown
  reaction       INTEGER NOT NULL DEFAULT 0,  -- reader turn the classifier marked as a reaction
  request_id     TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turns_book ON conversation_turns(book_id, atom, source_offset, turn_id);
```

- Read rule for both tables: `(atom, source_offset)` at or before the request position, same `incarnation`. The epoch is recorded but not filtered on, so a second pass sees earlier turns again once it reaches them.
- Stage 4 adds `books.blurb` and `books.blurb_source`. Not added now.
- Hosted port: migration `0018` with `owner_id`, row-level security and the same columns.

## Measurement

- Baseline first, then each stage, on the same cases. Reports keep "measured" and "inferred" in separate lists.
- New cases: Karamazov inside chapter 3 and inside chapter 5; Pride and Prejudice at a late position (about chapter 34 of 61) with whole-book questions ("what's happened so far?", "how has Darcy changed since the ball?") and forbidden later events.
- Note-building is scored on its own: sentences kept and dropped, and a reference review of kept sentences against the chapter.
- Transport: `codex-local` for the writer and second look, Jev through TypeSafe. Timings from `codex-local` are not production timings.
- Expected spend (inferred from the audit's $0.0006 of Jev per question; I will read the real per-call price from the cost ledger before the first run): ChatGPT subscription calls cost nothing extra; Jev under $0.25 for the baseline plus stage 1, with the script's $0.50 per-book ceiling as the stop.

## Read map (agreed 15 November)

The single read position is replaced, for Mothli, by a map of what the reader has read.

**Rule.** For each book Mothli keeps the stretches of text the reader has read and how it knows: read here, told by the reader, or imported. Only text inside the map reaches a model. Where the reader is now only tells Mothli what just happened.

| Case | Behaviour |
|---|---|
| Page by page | Each page left after enough time on it is added to the map |
| Skip to the middle | Only what is read from there on is in the map. Earlier chapters are a known gap; Mothli says so and never fills it from its own knowledge |
| Jumping around | The map is the union of what was read, in any order |
| Import from another app | Offered once on first open, as the reader's word. If accepted, everything before the chapter start at or before that point is marked as told |
| Re-reading | By default the map and the earlier conversation are kept. "Read it fresh" clears the map; earlier conversation reappears as its place is read again |

**Decisions.**

- A page counts as read when the reader leaves it after at least a quarter of ordinary reading time for its words (ordinary taken as 300 words a minute), or when it is on screen as they ask.
- One page report may cover at most 12,000 characters, so a faulty report cannot mark a book as read.
- "I've read this" and "I haven't read this" per chapter.
- A jump adds nothing to the map. The jump dialog offers "I've read everything up to here" separately.
- Mothli speaks first only at chapter ends and on the home screen.

**Data model (catalog.db).**

```sql
CREATE TABLE IF NOT EXISTS read_ranges (
  book_id      TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation  TEXT NOT NULL,
  atom         INTEGER NOT NULL,          -- chapter ordinal
  start_offset INTEGER NOT NULL,          -- in the chapter's normalized text
  end_offset   INTEGER NOT NULL,          -- exclusive
  source       TEXT NOT NULL CHECK (source IN ('read','told','import')),
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS read_position (       -- where the reader is now; may move backward
  book_id TEXT PRIMARY KEY REFERENCES books(book_id) ON DELETE CASCADE,
  incarnation TEXT NOT NULL, atom INTEGER NOT NULL, source_offset INTEGER NOT NULL, updated_at TEXT NOT NULL
);
```

- A chapter's notes are usable when the map covers the whole chapter; the hash rule already gives this.
- A stored turn is visible when the place it was written at is inside the map.
- The older chapter counter stays for the views that need one number (cast, recap, chapter closeout, chapter titles), and it now comes from the map: the last chapter up to which every chapter is finished, where finished means at least 80% read with the ending reached. A leap no longer moves it.
- Behind `READER_READ_MAP`, which follows streamed replies when unset and so is on wherever they are (audit section 6q).
