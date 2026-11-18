# ADR 0042 — Story memory in hosted mode

**Status:** Proposed (2026-11-17). Accepted when merged. Replaces the sketch of ADR 0041 section 4
("What builds it") and ADR 0041 part 10. Amends ADR 0024 (the extraction job) and, for library
editions built by the operator (section 3, the owner's decision of 17 November 2026), ADR 0017
section 3.

File and line references are to `main` at b51f786 (PR #100). Nothing in this record was run against a
database, a server or a model; every claim about behaviour comes from reading the code cited next to
it. The figures in "What was measured" come from the owner's runs and are labelled as such.

## Context

**Story memory** is what Mothli keeps about a book as it is read: the people and places (entities),
their other names (aliases), how they are tied (edges), what happened (events), themes, how a person
stands at a chapter (entity state) and a summary of each chapter. A language model extracts it one
chapter at a time, a second model call reviews every extracted statement against that chapter's text,
and only a chapter that passes is kept, with a **receipt** (a row saying "this chapter's memory is
complete") written last. It feeds the cast list, the story so far, "who was that?", and the fixed name
check (`score_recap`) that removes a sentence naming someone the reader has not met.

Local mode builds it (`backend/app/ingest/worker.py`, `backend/app/ingest/extraction/`). Hosted mode
does not: hosted chats run on an empty memory with the checks local mode applies to an unbuilt memory
(ADR 0041 section 4), the cast and story screens say they keep nothing for this book
(`frontend/src/reader/StoryMemory.tsx:46-50,193-194,261-262`, `AskTheBook.tsx:674-678`), and adding a
book makes no extraction job while `HOSTED_READER_KEYS` is off (`hosted/tenant/api.py:632`). The owner
has decided to build it before launch, so no reader's data has to be migrated later. This record is
the design the builders follow.

### What was measured (the owner's runs, 17 November 2026)

The first eight chapters of *Pride and Prejudice* (65,021 of the book's 685,566 characters), local
mode, one chapter at a time, writer `gpt-6.1-terra` through the owner's ChatGPT subscription,
embeddings stubbed, the optional Jev screen of extraction off (its default, `config.py:48`). Dollars
are the app's own price list (`cost/limits.py:21-39`: terra $1.00 per million input tokens and $5.00
per million output; `gpt-6-luna` $0.10 and $0.50) applied to measured tokens.

The subscription's command-line tool adds about 7,780 input tokens of its own to every call (measured
by the owner: a ten-token prompt reports 7,781 input tokens on terra and 7,777 on luna). That overhead
does not exist on a paid API, so it is taken out below: 63 calls × about 7,780 = about 490,000 of the
912,609 input tokens the run reported.

| Phase (63 calls in all) | Calls | Input tokens | Output tokens | At terra's price |
|---|---|---|---|---|
| `extraction` | 8 | about 81,300 | 15,671 | $0.16 |
| `extraction-partial` (extractions rejected and repaired) | 5 | about 16,000 | 5,888 | $0.05 |
| `extraction-review` | 50 | about 325,200 (about 6,500 a call) | 32,515 (about 650 a call) | $0.49 |
| **Total** | 63 | **about 422,500** | **54,074** | **about $0.69** |

So: about 53,000 input and 6,800 output tokens per chapter, about 6.5 input tokens per character of
book text, **about $0.09 a chapter**, and, scaled on characters (× 10.54), **about $7.30 for the
novel** (4.46 million input tokens, $4.46; 0.57 million output, $2.85). Output is 39% of the cost, so
no input discount can remove it. The review is 70% of the cost. A chapter's text is sent about eight
times: once to extract, then once with every review batch (below). These are the book's first, short
chapters; the roster of known people is sent with every call and grows through a book, so scaling on
characters probably understates later chapters (not measured). 21 minutes for the eight chapters
through the subscription's slow transport; speed on a paid API is not measured.

A second run set `LLM_CHEAP_MODEL=gpt-6-luna` and left the large model at terra. Chapter 1 was accepted
(6 calls). Chapter 2 ended with `ExtractionGroundingRejected` after one extraction and two repairs on
luna (reviews on terra), and the book's extraction stopped there. One book, two chapters: a small
sample, but a hard stop. (Mothli's own subscription transport refuses every model but terra,
`llm/codex_local.py:44-45`; the owner ran luna outside that check. Not repeated here.)

No prompt caching was counted (the subscription reports none). Caching and speed stay unmeasured
without a paid account, which the owner does not want; the rule is tokens measured on the
subscription, overhead removed, times the public price list.

### What the code says, read for this record

1. **The review is on the large tier, not the cheap one.** The extraction call asks for
   `tier="cheap"` (`ingest/worker.py:544`); every review batch asks for `tier="large"`
   (`ingest/extraction/validation.py:379-382`), and the identity review too (`identity.py:149`). The
   large tier is also the chat writer's (`stream_conversation.py:452`) and the notes writer's
   (`chapter_notes.py:221`). Both tiers default to terra (`llm/client.py:46`). A cheaper "cheap"
   model therefore cheapens only the 30% of the cost that is extraction and repairs.
2. **Why a chapter is sent about eight times.** `review_extraction` splits the fields to check into
   batches of at most 24 segments (`validation.py:351`: `max(8, min(24, (4096 - 512) // 128))` at the
   default 4,096-token output ceiling, `config.py:113`), at most 16 batches (`:373`), and each batch's
   prompt carries the whole chapter, the roster and the whole extraction again (`:352-354`).
3. **Extraction is strictly in order.** Chapter *n* is extracted against the roster of chapters
   1 to *n*−1 (`worker.py:658-664`, `pipeline.py:37-46`), and the commit refuses a roster that changed
   meanwhile (`pipeline.py:159-160`). A chapter cannot be skipped and done later without redoing what
   follows it.
4. **A rejected chapter stops the book's memory there, and locally is paid for again.** A grounding
   rejection after the one allowed repair (`worker.py:446-459`) propagates out of `_ingest_upto`; the
   worker records an error (`worker.py:353-358`) and later chapters are never reached. Every later
   enqueue (each page turn that finishes a chapter, `api/reading.py:143-146`; every position report,
   `:243-246`; "I've read this", `:176-180`) starts again at the same chapter and pays for it again.
   This is local behaviour today; hosted mode must not copy it (section 2).
5. **Changing the extraction model stops a book.** The first chapter pins the extractor
   (`provider:cheap model`) and the embedder (`pipeline.py:49-57`); a later client with another
   extractor or embedder raises `ModelMigrationRequired` before any paid call (`worker.py:685`,
   `llm/versioning.py:90-112`), meaning "extract the whole book again". The extraction model chosen at
   launch is the one every hosted book keeps.
6. **Local defaults would stop this novel.** The local per-book ceilings are $5.00 and 500,000 output
   tokens (`config.py:116-118`); at terra this novel needs about $7.30 and 570,000 output tokens. The
   eight-chapter run did not reach them. Hosted mode ignores the per-book ceilings
   (`hosted/companion/spend.py:28-30`).
7. **The hosted pieces that exist.** `PostgresWorkerRepository.commit_chapter` (`hosted/jobs.py:795-1003`)
   writes, in one fenced transaction, the chapter row, its summary, one cost-ledger row and the
   receipt last, and nothing else: no entities, aliases, edges, events, participants, themes or
   entity state. It stores the chapter key as the chapter's title (`:906`), and writes its own
   `extraction` ledger row (`:936-957`) and expects an `extraction`-phase reservation (`:858-864`),
   while the companion's spend adapter records every writer call as phase `synthesis`
   (`spend.py:272`): the two spend paths do not agree. The memory tables exist with owner-composite
   keys and receipt-last deferred foreign keys (`schema/0002_library_memory_and_vectors.sql:85-373`).
   `HostedBookStore` reads chapters, receipts, entities, aliases, themes, events and summaries
   (`hosted/companion/memory.py:42-109`), not edges or entity state.
8. **No extraction handler exists at all.** `ingest_book` appears only in the schema and the tenant
   repository; the worker's one dispatcher refuses it (`hosted/companion/jobs.py:209,263-265`). The
   reader-key extraction path of ADR 0024/0026 (`waiting_configuration`, `tenant/repository.py:1938-1953`)
   never ran in any build.
9. **Hosted memory can already be changed by the reader.** `POST /api/books/{id}/memory-corrections`
   (`tenant/api.py:1655-1679`) writes entities, aliases, edges, state and corrections in Postgres
   (`tenant/repository.py:1667-1739`), which is why the tenant role holds `INSERT` on those tables
   (`tenant/grants.py:23-29`). Any working copy of memory kept outside Postgres would miss them.
10. **Content hashes differ in length.** Local chapters are fingerprinted with the first 16 hex
    characters of SHA-256 (`ingest/extraction/chapter_text.py:54-57`); hosted `chapters.content_hash`
    must be 64 (`0002:97`); the local-archive importer hashes the raw text in full
    (`hosted/local_migration.py:311-314`).
11. **The hosted chapter counter can run ahead of the read map** (open items, line 159: a stale
    device's position report raises it). The cast and story views follow the counter locally
    (`read_map.py:103-104`); in hosted mode the extraction target must not.

## Decision

### 1. The seam: the unchanged extraction runs on a throw-away working copy; one fenced publish

**What must run unchanged.** Everything that decides what is extracted, what passes review and at
which chapter a fact becomes visible: the extraction and its repair (`worker.py:432-617`), the review
(`validation.py`), identity resolution and preparation (`pipeline.prepare_chapter`, `:67-131`), and the
write that stamps every row with its chapter (`pipeline.ingest_chapter` and `_write_chapter`,
`:134-294`). The modules that decide what a reader is shown (`score_recap`, `same_person_names`,
`completion_frontier`, the bookmark-bounded views) also run unchanged, over `HostedBookStore`.

**Options considered.**

- (a) **A persistent SQLite memory file per book on the worker's disk** (ADR 0041's sketch), published
  to Postgres after each chapter. Rejected: reader corrections are written in Postgres (fact 9) and
  would never reach the file, so the next chapter would be extracted against a roster the reader has
  corrected; account erasure and book removal find rows from the database catalogue
  (`hosted/purge.py:43-84`) and would not know the file exists; a second worker process or a lost
  disk would have a different or no file. It is a second home for memory.
- (b) **A Postgres implementation of the ~20 `MemoryDB` write methods** `_write_chapter` calls.
  Rejected: it rewrites, in a second language, the code that stamps `revealed_at` and closes earlier
  states (`pipeline.py:258-263`), which is spoiler logic.
- (c) **A throw-away working copy rebuilt from Postgres for each chapter.** Chosen.

**Decision (c).** For each chapter the handler:

1. builds an empty local `MemoryDB` for the book in a private working location and copies into it,
   from this owner's Postgres rows (read through the tenant role, which can already `SELECT` them,
   `grants.py:20-30`), what extraction of chapter *n* reads: the live chapters and receipts 1 to
   *n*−1, entities, aliases, entity state and corrections, and the pinned identity. Postgres UUIDs map
   to local integer ids and back. The hydrator checks each copied chapter's 64-hex hash against
   SHA-256 of the chapter text from `HostedBookSource`, so a stale row fails closed;
2. runs the unchanged extraction, review, preparation and `ingest_chapter` for chapter *n* against it;
3. reads back exactly what that chapter added (rows stamped *n*, and the `invalid_at` that
   `replace_state` set on an earlier state) and **publishes** them through one new worker-role method,
   `PostgresWorkerRepository.commit_chapter_memory`, in one transaction fenced by the job's lease (the
   `_locked_lease` check of `jobs.py:465-489`), rows in extraction order with distinct `recorded_at`
   (open items, line 166), the receipt last (deferred foreign keys, `0002:149-151` and the like). The
   same transaction refuses if any memory row of this book was recorded after the copy was taken (a
   correction arrived meanwhile): the job then rebuilds and prepares again, as local mode does on
   `StalePreparedRoster` (`worker.py:768-800`);
4. deletes the working copy.

**What is never written twice, and never half.** The receipt has a primary key on the chapter
(`0002:124`) and the chapter row is unique per key (`0002:103`), so a second publish for the same
chapter fails and rolls back whole; a publish is one transaction with the receipt last, so nothing is
seen before it is complete (`HostedBookStore` loads every table in one statement, `memory.py:19-21`).
Raw chapter text, which `_write_chapter` stores in the working copy (`pipeline.py:213`), is never
published (ADR 0019, reviewed difference 1); nor are passages or vectors (section 8). Chapter rows
carry the manifest keys `<book id>:<file>` that `HostedBookSource` produces (open items, line 167), and
the real title, file and part label, not the key as title (fact 7).

**The one shared-code change, behaviour-free.** The per-chapter body of `IngestWorker._ingest_upto`
(`worker.py:646-814`) and the three extraction methods it calls (`:432-617`) move verbatim into a new
`app/ingest/chapter_extraction.py` (`ChapterExtraction(catalog, client, settings, check_stopping,
sleep)`), which `IngestWorker` calls. The local suite must pass unchanged. The hosted handler calls the
same class with the hosted catalog, the platform's extraction client and `check_stopping` = the lease
check. The local worker's file path for the source (`worker.py:370`) and its per-book thread state stay
local.

**What the hosted catalog must add** for the moved code: `cost_reservation_ids(book_id, phase=,
chapter_ordinal=)` (`worker.py:673-679`) over this owner's open companion reservations, and
`finalize_ingest` (`:666,809`), which in hosted mode only settles the chapter's deferred extraction
reservations (the receipt is already published). `get_state()["ingest_progress"]` already counts
contiguous receipts (`hosted/companion/store.py:61-102`).

**Costs settle once.** The extraction call's reservation is deferred (`worker.py:547`) and its review
calls settle at once (`cost/limits.py:266-267`). After the publish, the handler settles the deferred
ones through the tenant spend adapter, exactly as local mode settles after its memory commit
(`worker.py:809-811`). A crash between the two leaves them open; the next run finds the receipt and
settles them at their noted size (`spend.py:333-354` has already replaced the estimate with the
actual). `commit_chapter_memory` writes no ledger row of its own; `commit_chapter` stays, unused by
this path, for the retired reader-key path (section 6).

### 2. When a chapter is processed, and for whom

**Only what this reader has read, in order, never ahead.** The target is the read map's
`finished_through` (the last chapter up to which every chapter is finished, `read_map.py:97-108`),
taken from `read_snapshot` through the hosted adapters at the start of each chapter, not the chapter
counter (fact 11). An unread book costs nothing and nothing about its unread chapters is computed for
this reader. A skipped chapter holds everything after it, as locally: chapters are extracted in order
(fact 3), and the read map's own "were these read?" question (`skipped_before`,
`hosted/companion/routes.py:116-118`) is how a reader fills the gap.

**Triggers.** Where hosted routes already ask for chapter notes (`routes.py:127-128,148-149`), they also
ask for story memory: a page that finishes a chapter, and "I've read this". The shared stand-in in the
streamed chat needs nothing. Books added before this lands get their first job from the first finished
chapter.

**A reader who jumps ahead or marks chapters read** raises `finished_through`; the job works through
every chapter up to it, one per run, each paid within the allowance (section 4). **Marking chapters not
read** lowers the target; work already queued stops at the next chapter boundary, and memory already
built stays (it is shown only up to the reader's position, as locally after a re-read).

**A chapter that is rejected.** Local mode stops the book's memory at that chapter and pays for it
again on every enqueue (fact 4). Hosted mode: a chapter rejected on two separate runs (each run is one
extraction plus one repair, as locally) is published as an **unremembered chapter**: the existing empty
extraction (`worker.py:135-140`, which `review_extraction` clears without a model call,
`validation.py:338-340`), with a marker the story view shows ("I couldn't make notes on chapter 9").
Extraction then continues. Why this is never looser than local mode: an empty chapter asserts nothing;
a person first met in it is created when next named, stamped at that later chapter, so for a reader
between the two the name counts as not yet met and the fixed check removes it (stricter, not looser);
and local mode in the same state has no memory at all from that chapter on, so it blocks fewer names
than this. What is lost: that chapter's summary and facts, and "met in chapter" is late for its
people. The count of rejections is kept in the job rows (a new failure code, section 6), so it needs no
new table. Which column carries the marker (`chapters.kind`, `0002:96`, is the candidate) is the
builder's; whether anything reads `kind` today was not verified.

**What the reader sees meanwhile.** The views show memory through the last processed chapter
(`min(finished_through, completion frontier)`, as the local clamp `api/views.py:181-195`). The app
already polls `GET /api/books/{id}/ingest` while `status` is `running` and refreshes the cast and
story as `ingest_progress` grows (`frontend/src/reader/memoryCatchUp.ts:9-38`,
`ReaderPage.tsx:358-388`); hosted mode serves that route (section 9). A chapter waiting for tomorrow's
allowance answers `status: "waiting"`, and the panels say once, in Mothli's voice, that Mothli will
catch up on the rest tomorrow. The chat never waits for memory.

### 3. Library books: built once, handed out only up to each reader's place (decided)

**The owner's decision (17 November 2026):** "build once and hand it out only up to each reader's
place." His test: "Shouldn't Mothli mostly know only about the current user and what they've read?"
Ten readers of one book, at chapters 2, 5 and 10: whatever answers the reader at chapter 2 must not
hold chapters 5 or 10.

**What the part of Mothli answering one reader can hold, under each design.** "Holds" means: is
loaded into the web process for that reader's request.

| | Per reader (A) | The door (decided) | Unclamped sharing (rejected) |
|---|---|---|---|
| Who pays for a chapter | that reader, once for each reader | the platform, once for everyone | the platform, once |
| Memory of chapters past the reader's place | none in the usual case; some after "I haven't read this" (built while the reader had read them) | none, ever: it never leaves the database | the whole book, on every request |
| What keeps it from the reader | it was never computed for them, and every view clamps | the database hands out only the reader's prefix, and every view clamps | the view's clamp alone |
| Reveals one reader to another | nothing | nothing (built ahead of time, see below) | nothing |

**(A) Per reader.** Built first (parts 1 to 3), and used for uploads and for any library title not
built ahead. Every row carries the reader's `owner_id`; row-level security, the two-readers tests,
erasure, removal and export work as for every other hosted row. About $0.09 a chapter, $7.30 for this
novel, again for each reader. **Whether it is switched on, and with what allowance, is still the
owner's open question** (section 4). It is a setting, `HOSTED_READER_STORY_MEMORY`, **off unless set**.
While it is off, a book with no library memory (every upload, and any library title not built) has
none: the chat runs on an empty memory with the checks ADR 0041 section 4 describes, the cast and story
routes answer 404 for that book, and the panels show the lines they show today ("I don't keep a list of
who's who for this book yet. Ask me about anyone you've met in the chat.",
`frontend/src/reader/StoryMemory.tsx:193-194,261-262`; "who was that?" says the same,
`AskTheBook.tsx:674-678`). No job is made and nothing is spent.

**(B) The door: library memory built once.**

- *Built by the operator, never by a reader.* An operator command builds the whole book for a fixed
  list of free-library titles, as the **library account** (one reserved user row that never signs in),
  with the same job, handler and publish as (A). Its spend is the platform's. Because whether a title
  has memory is the operator's choice, made before anyone reads it and the same for everyone, it tells no
  reader anything about another. Building on demand when the first reader gets there is rejected: a
  cast appearing at once would tell a reader someone else got there first, the existence signal ADR 0017
  section 3 forbids.
- *Keyed by the exact file.* A reader's book uses the library memory only when it came from the free
  library (`books.source_kind = 'gutenberg:<number>'`, hosted part 11) and its `file_hash` (SHA-256 of
  the EPUB, `0002:25`) equals the built edition's. A reader whose file differs (Project Gutenberg changed
  it) gets (A) or nothing. Uploads are never matched to it: matching a private file to someone else's
  would reveal what a person owns.
- *The door hands out the reader's prefix, decided in the database.* The library account's rows sit in
  the ordinary memory tables under its own `owner_id`, so row-level security hides all of them from every
  reader. One `SECURITY DEFINER` SQL function (the pattern of the two spend functions,
  `schema/0020_companion_spend.sql`, `grants.py:76-79`), `library_story_memory(book uuid)`, is the only
  way in. It takes **the book and nothing else**; there is no position argument. It:
  1. finds the calling reader from the transaction's owner setting (`app_current_owner_id()`, the
     same setting row-level security uses, set by the tenant adapter in every transaction, ADR 0041
     section 2), and that reader's live book with that id; refuses unless its `source_kind` and
     `file_hash` match a built edition;
  2. works out the reader's place from the reader's own rows: **`reading_state.memory_through`**, a new
     column (migration 0026) that holds the read map's `finished_through` and nothing else, capped by
     the highest chapter in which the reader has any `read_ranges` row (a backstop the database can check
     on its own: the place can never be past every chapter the reader has a recorded page in);
  3. returns, in the payload shape `HostedBookStore` already reads (`memory.py:42-109`), only rows whose
     chapter stamp is at or before that place: chapters, receipts, entities, aliases, themes, events,
     summaries, and (section 9) edges and entity state. An `invalid_at` past the place is returned as
     empty, because "this state ends at chapter 7" is itself news about chapter 7; an earlier state's
     end is handed out only when the chapter that ended it is within the place. The same holds for
     entities, edges, themes and summaries, which also carry `invalid_at`.

  Rows of later chapters never leave the database for that reader's request.
- *Why the place cannot be pushed ahead by a stale device (fact 11).* The device position route raises
  `reading_state.bookmark` (`tenant/repository.py:1189`, `SET bookmark=GREATEST(bookmark,%s)`), which is
  why the counter can run ahead. `memory_through` is written by no route a device calls: only the hosted
  adapter's `set_bookmark` writes it, and its only caller is the read map's `sync_counter`
  (`read_map.py:107-108`, `hosted/companion/store.py:104-118`), which writes `finished_through`
  computed from pages checked against the source (`read_map.py:56-72`). One thing makes this harder than
  it sounds: `sync_counter` writes only when the counter it reads differs from `finished_through`
  (`read_map.py:107`), so if the hosted adapter kept reading the device-raised column, a stale device could
  leave `memory_through` behind (stricter, but stuck). The part therefore makes the hosted adapter's
  `bookmark` (in `get_state` and `set_bookmark`) be `memory_through`, leaving the device-raised column to
  the device position route alone. That also closes open item 159 for the companion. To be proved by a
  test, since it changes what the companion reads as the counter.
- *Why not recompute `finished_through` in SQL.* It depends on the read share and the pages that are not
  part of the book proper (`read_map.py:60-72`, `FINISHED_SHARE`, `is_content`): a second copy of the
  read-map rule in another language, which is what this design keeps out. The column holds the one
  computed value; the database adds only the coarse backstop of step 2.
- *Moving back and forward.* "I haven't read this" lowers `finished_through`; `sync_counter` writes it;
  the next request gets less. Reading on, more. There is no per-reader copy to keep in step.
- *A reader ahead of what has been built* (a title still being built, or stopped at an unremembered
  chapter): the door returns what exists up to their place; the views and the chat treat the rest as
  unbuilt, as for (A).
- *Progress is the reader's prefix too.* For such a book, `get_state`'s `ingest_progress`
  (`store.py:83-99`, today read from the reader's own receipts, which a shared book does not have) and
  `completion_frontier` (`memory.py:224-231`) are computed from the receipts the door returns, so they
  stop at the reader's place; `GET /ingest` reports the same, with `status: "done"` once the edition is
  built. Without this change the chat would see progress 0 and use no memory at all
  (`stream_conversation.py:587-588`).
- *What the door costs.* It is the same single statement as today's load (`memory.py:19-21`: every table
  at one database moment) with a `revealed_at <= place` predicate added to each part, so a reader at
  chapter 2 loads less than today's whole-book load, never more. The memory tables already have indexes
  that start with owner, book, incarnation and the chapter stamp (`0002:180-182,205-207,237-239,263-265,
  315-317`, partial on live rows); the load also reads retracted rows for `_audit_all`, which those
  partial indexes do not cover. Whether the planner uses them for the library account's rows was not
  verified; part 5 checks it with `EXPLAIN` on a built novel and adds an index only if it must. The
  door runs once per request (as the load does today).
- *Corrections.* A reader cannot change shared rows: the correction route answers that this book's
  memory cannot be corrected. The hosted `GET /memory` and `GET /memory-corrections` routes read the
  reader's own rows (`tenant/repository.py:1270,1435`) and so show nothing for such a book.
- *Deleting an account or removing a book* removes the reader's own rows and their book row; the
  library's rows hang from the library account's chapters (migration 0024 cascades every memory row from
  its chapter's receipt) and are not touched. The purge finds nothing of the reader among them.
- *Amendments.* ADR 0017 section 3 ("no shared derived memory") is amended for library editions built by
  the operator. The privacy text says that notes on free-library books are prepared once for everyone.
- *Cost.* Once per title: about $7.30 for a book of this length at terra's list price (roughly $730 for a
  hundred titles of similar length, by arithmetic), then nothing per reader.

**What the door changes for the checks, and the question of later names.** Read from the code:

- The fixed check `score_recap` splits every name in memory into "met" (stamped at or before the
  checking point) and "forbidden" (stamped later), reading every row on purpose
  (`eval/spoiler_gate/synthesis.py:437-473`); the chat drops a sentence with a forbidden name the reader
  has not read (`stream_conversation.py:383-391`). `same_person_names` uses met names only
  (`known_names.py:16-41`). The chat's other unread-name guard looks at the reader's question against
  the read text, not memory (`stream_conversation.py:579-581`).
- The checking point is `min(counter, built chapters, read_through, completion frontier)`
  (`stream_conversation.py:587-588`). Per-reader memory is built only up to `finished_through`
  (section 2), so in the usual case **it holds no names past the checking point either**: the forbidden
  list is empty, and a later name is caught by Jev and the second look, exactly as with an empty memory
  (open items, line 193). Per-reader memory holds later names only after a reader marks chapters not
  read (it keeps what was built while they were read).
- So the door is **as strong as per-reader memory** for the fixed name check in the usual case, and
  weaker in one case: after "I haven't read this", per-reader memory still blocks names first met in the
  un-marked chapters; the door no longer hands them out. Those chapters' text is also no longer given
  to the writer (the read map removes it), so such a name could reach a reply only from the model's own
  knowledge, and Jev and the second look still judge every sentence.
- The unclamped design was **stronger** than both: it would block, deterministically, a later name the
  writer knows from its own reading of a famous novel. That is the real gap for library classics, and
  per-reader memory has it too.

Two versions for the owner, plainly:

1. **No later data at all (recommended).** The door returns the prefix and nothing else. It passes the
   owner's test exactly; it is as protective as per-reader memory except in the "I haven't read this"
   case above.
2. **The prefix plus a bare list of later names, only for blocking.** The door would also return the name
   forms (canonical names and aliases) stamped after the place, with no chapter, no fact, no relation,
   no theme, as a list the fixed check uses only as "forbidden". It would close the gap for names the
   model remembers. What it would reveal if it leaked into a prompt, a log or an error: who appears later
   in the book, which is itself a spoiler (the code's own example is a future alias, "Siberia",
   `synthesis.py:413-414`). Today nothing sends the forbidden list to a model (`names_note` and the
   identities use met names only, `known_names.py:44-51`, `stream_conversation.py:589-591`), and hosted
   logs allow reviewed static messages only (ADR 0041 part 6, item 11); but the list would sit in the
   web process for every request, against the owner's test.

Recommendation: **version 1**, and measure. Part 5 runs the chat's spoiler probes on library classics
with a stand-in writer that names a later character the way a model remembering the novel would; if Jev
and the second look let such names through, version 2 comes back to the owner as its own decision with
those results.

### 4. Money

**Model.** Extraction and review both on **terra**, as measured; no Jev screen, no identity review
(`config.py:48-49`, their defaults; what was measured). Luna is not used for extraction: it stopped the
one book it was tried on at chapter 2, and the extractor is pinned per book, so a later change means
paying for every book again (fact 5). Luna on extraction alone would have saved about a quarter (the
extraction and repair calls are $0.21 of the $0.69; at luna's prices they would be about $0.02, so
about $0.51 for the eight chapters and $5.40 for the novel, list arithmetic), which is not worth a
hard stop. Moving the **review** to a cheaper model is not proposed: the review is what catches an
extraction that imports later plot from the model's own knowledge of a famous novel (`validation.py:96-99`,
`:134`), and its summaries are shown verbatim in the story view, so a weaker reviewer is a spoiler risk,
not only an accuracy one. The extraction worker builds its own client (cheap and large both terra,
`llm/client.py:107-118`, Standard processing, the stub embedder, section 8), so the chat's writer
setting never changes extraction and the reverse.

**One allowance, one rule.** Extraction spend goes through the same `reserve_cost` with its local phase
names as `purpose` (`extraction`, `extraction-review`, `extraction-partial`, ...), so it is counted by
the one definition of a reader's spent day and month that both `reserve_cost` and the worker's claim
read (`spend.py:55-75,114-139`; `hosted/jobs.py:319-329`). No second rule, no second sum.

**A chapter is admitted whole, like a reply.** Background calls are checked one by one today
(`spend.py:21-22`). A chapter costs about $0.09, close to the whole $0.10 day: checked call by call,
a chapter started with a few cents spent would be refused half-way, its paid calls thrown away
(settled as `extraction-partial`, `worker.py:613-617`), and tried again the next day the same way. So
the handler uses a catalog like `ReplyCatalog` (`hosted/companion/store.py:177-190`): the chapter's
first call is checked against the caps; once admitted, its later calls are not refused for a dollar
cap. No change to `spend.py`: it already reads `_admitted` (`spend.py:274-275,300-320`). A reader can
end a day past the cap by at most one chapter (one job runs per reader and book, section 6). The
platform's day is still charged with every call; its check is skipped only for a chapter already
admitted, as for a reply.

**At a cap** the book stays readable and chattable, the screens show what is built, and the claim moves
the job to the next UTC day (`jobs.py:387-402`, once the new kind is a companion kind).

**What the choices mean, by the measured figures** (terra list price; unmeasured where said):

| | Per chapter | Reader reading 5 chapters a day | One novel of this length |
|---|---|---|---|
| Per reader (A) | about $0.09 | about $0.43 a day, about $13 a month | about $7.30 |
| Library title through the door (B) | $0 to the reader | $0 | about $7.30 once, for everyone |
| With prompt caching (a guess, unmeasured) | | | the owner's guess: $5 to $7.50 |

By arithmetic, the floor if every review input token were billed at terra's cached price ($0.05 per
million, `limits.py:38`) is about $4.05 a novel: output alone is $2.85. Caching cannot be measured on
the subscription. Today's caps ($0.10 a day, $2.00 a month, `config.py:166-168`) cover about one
chapter a day and about 23 chapters a month of per-reader memory with no chat at all. The platform day
($5.00) covers about 57 chapters across all readers, about two thirds of this novel. **So the caps and
(A) do not fit together.** Library titles go through the door (decided) and cost readers nothing. For
uploads the owner still chooses: leave `HOSTED_READER_STORY_MEMORY` off (uploads have no story memory;
section 3 says what the reader sees), or turn it on with caps that fit (about $0.50 a day and $10 a
month for a reader of one such novel a month, plus chat), or turn it on and let the day cap pace it
(about one chapter a day at today's caps). The code is the same in every case. Library editions are
built under the library account, which is an owner like any other to the spend code: it has its own
`owner_limits` row, whose day and month caps the operator sets for the build with the existing tool
(`python -m app.hosted.limits set`, ADR 0027), so no reader's allowance is touched and no new rule is
added. The platform's day cap still applies to it, so a build of many titles is spread over days or run
with that cap raised on purpose.

**What must be measured before the numbers are settled**, all on the subscription, overhead removed:
three more books of different lengths and kinds, all chapters, to see later chapters' cost as the roster
grows and how often terra rejects a chapter; the same with the real embedder if section 8 is ever
reconsidered. Unmeasurable without a paid account: caching and speed. **The first week's real
figures** are in the ledger: cost per chapter and per thousand characters, by purpose, from
`cost_ledger` rows with `purpose LIKE 'extraction%'` grouped by owner, book and `chapter_ordinal`, and
time per chapter from `job_attempts`. The operator records them in the open items within the first
week, and the caps are reset from them.

### 5. Cutting the cost of the pipeline itself (proposals, each to be proved before it replaces anything)

None of these makes memory less checked than local mode's; each changes how the same checks are paid.
None is part of the first build.

1. **Larger review batches.** Today at most 24 segments a batch (`validation.py:351`), so a chapter's
   text goes out about seven times for review. Doubling the batch to 48 would send it about four times:
   review input falls by roughly 40% (about $0.13 of the $0.69, arithmetic), output stays. Risk: long
   audits are why batches exist ("exceeded the output ceiling or omitted fields", `:348-350`); the
   coverage check (`_validate_field_evidence`, `:207-230`) still refuses an incomplete audit, so the
   risk is more refused-and-repaid batches, not less checking. Proof: the structural coverage tests
   with stand-in models, then the same books measured on the subscription, comparing calls, repairs and
   rejections. Needs the output ceiling raised for review calls only.
2. **Keep the review prompt a cacheable prefix.** It already almost is: the system text is the same for
   every batch and the user JSON puts the chapter, the roster and the extraction before the batch's
   fields (`validation.py:313-328,352-354`). Two things break the shared prefix: the format repair
   appends to the system text (`:380`), and `source_id_rules` follows the fields. Moving the repair line
   to the end of the user text and the rules before the fields keeps the prefix for every call. The app
   already prices cached reads on the native API (`llm/client.py:308-313`, `limits.py:69-87`). Saving:
   unmeasured; the floor is above. Risk: none to checking (same text, same order of meaning); proof: a
   test that every batch's prompt shares the prefix byte for byte, and the review tests unchanged.
3. **Keep a cleared chunk's clearance across a crash.** Cleared chunks are reused within one run
   (`worker.py:436-438`) but lost if the run dies mid-chapter, and the whole chapter is paid again.
   Saving: at most one chapter per crash. Risk: low (a clearance binds exact bytes, `validation.py:241-249`);
   needs a table of derived content per owner, which erasure covers by its `owner_id`. Not worth it
   until crashes are seen.

Rejected: reviewing a sample of fields, skipping review of "simple" chapters, and reusing a review
across readers outside design (B). Each lets an unchecked statement into memory.

### 6. Jobs

**Kind.** A new kind, `story_memory`, on the existing table and worker; not `ingest_book`. `ingest_book`
is one whole-book job with a per-incarnation idempotency key (`tenant/repository.py:2044`, unique per
owner, `0003:72`), counted by the upload quota and listed by `GET /api/jobs` (`repository.py:1829-1831`);
it belongs to the reader-key path.

**One waiting, one running, one chapter per run.** `story_memory` joins `COMPANION_KINDS`
(`hosted/jobs.py:29`) and the pending-job unique index (`0021:37-39`), so at most one waits and one runs
per owner and book (`jobs.py:362-370`), and the claim skips paused providers and capped readers
(`:387-402`). A run does one chapter, then asks for the next job if the target is not reached. One
chapter per run lets the claim's order (`priority`, then `run_after`, `:371`) take turns between
readers on one worker. Priority 5: below chapter notes and recollections (10,
`hosted/companion/jobs.py:51`), above the old `ingest_book` (0). The owner's `max_provider_concurrency`
(2) counts it beside notes (`jobs.py:403-417`).

**Leases.** The dispatcher's `_Lease` renews the two-minute lease before every paid call
(`hosted/companion/jobs.py:150-206`); `commit_chapter_memory` checks the lease in its own transaction.
A run that lost its lease reserves nothing more and cannot publish; the most it can waste is the calls
of one chapter. A cancelled job (its book removed: `repository.py:2652-2665` already cancels every job
of the book) stops the same way.

**Provider pauses.** Only the checker's refusals pause today (open items, line 201). Extraction calls the
writer only, so the handler also pauses `openai-compatible` (`provider_pauses` allows it, `0021:45`) on
the writer's refusals of the platform's account. Which `ProviderUnavailable` kinds mean "refused
account" was not verified; the part must find them in `llm/client.py`.

**Failures.** A grounding rejection gets its own failure code, `grounding_rejected`, non-retryable, so
two of them for the same book and chapter (the chapter is in `payload_metadata`, which forbids only
credential-shaped keys, `0008:24-29`) make the next run publish the unremembered chapter (section 2).
Other failures are as for notes (`hosted/companion/jobs.py:272-302`).

**Restarts.** Nothing is held in memory: waiting work is rows, a running job's lease expires and it
returns to waiting (ADR 0024). The working copy holds chapter text; it lives in memory if `MemoryDB`
accepts an in-memory database (not verified), otherwise in a directory only the worker's user can read,
emptied at worker start.

**Where it runs.** The companion dispatcher (`CompanionJobs`) gains the kind, behind a new setting
`HOSTED_STORY_MEMORY` (default off until this lands; it turns on the job kind, the routes of section 9
and the door), so one handler and one image serve all background work; the deployment runs two worker
processes so a chapter of two or three minutes does not hold up notes (the claim is safe across
processes, ADR 0024). A second setting, `HOSTED_READER_STORY_MEMORY` (off unless set, section 3), decides
whether readers' own books (uploads, and library titles with no built edition) get jobs at all; with it
off, the triggers of section 2 add nothing for such a book. A reader's book that matches a built edition
never gets a job.

**Library builds.** The operator command creates the library account once, adds an edition as that
account's book (the same import path as a free-library add, `store_epub`), and inserts one
`story_memory` job for it whose target is the book's last chapter instead of a read map (the library
account reads nothing). The job then runs chapter by chapter like any other, at the claim's turn, within
the library account's own caps.

**The reader-key path is retired for extraction.** No `ingest_book` handler ever existed (fact 8). With
story memory on, `store_epub` creates no `ingest_book` job in either mode; the hidden credential and
provider-settings code stays as ADR 0041 decided, and old `ingest_book` rows remain listable and
cancellable. (ADR 0043 step 7 then retired the kind: its rows are deleted by migration 0028, and
`GET /api/jobs` with its cancel route is gone.) ADR 0026's "ingestion waits for a validated extraction selection" no longer describes any
running path.

### 7. What already exists must cover the new rows without anyone remembering

| What | What the builders must do |
|---|---|
| Account erasure after 30 days (`hosted/purge.py`) | Nothing in the code: it erases every row of every table with `owner_id`, found in the catalogue, children first by foreign key (`purge.py:43-84`). The one table this design adds, `library_account` (part 5), names the library account in a column deliberately not called `owner_id`, so the purge neither lists it nor gains a privilege on it; the library account itself is never erased. Add one test: a reader with story memory is erased entirely. |
| Removing a book (migration `0024_erase_a_removed_book.sql`, PR #102, about to merge) | It makes every memory table cascade from its chapter's receipt, and the receipt already cascades from the chapter, so deleting a removed book's `chapters` erases its story memory; the tenant role gains `DELETE` on `chapters`. Checked against this design: the publish keeps the receipt last and every foreign key stays deferred, so the order of section 1 still holds; an unremembered chapter is a chapter and a receipt like any other; a row that `replace_state` closes hangs from its own earlier chapter. Rows that point at an entity (aliases, edges, participants, state, `0002:197-198,226-229,280-283,334-335`) do not cascade from the entity, so deleting **one** chapter whose entity a later chapter's rows name would fail: nothing deletes one chapter (removal deletes them all; an edition rebuild deletes the whole library book), and the test must keep it so. The library account's rows hang from the library account's chapters, which a reader's removal (owner predicate plus forced row-level security) cannot reach. The removal test must seed story memory rows. |
| "Take a copy of my data" (being built) | Export story memory only as the reader's view at their current place (the payloads of section 9), never rows: a per-reader book's rows can include chapters past the place (after "I haven't read this"). For a library book, the view through the door, which holds nothing past the place anyway. |
| Route inventory and the walk that refuses an account waiting to be erased | Each new route goes into `endpoints.json` with its two-readers test and the version is bumped (20 today, `endpoints.json:2`, `tests/hosted/test_tenant.py:118`). The walk tries every enabled route (`tests/hosted/test_account_deletion_postgres.py:358-387`), so the new routes are covered by using `_owner` like the others. A background resource `story-memory` is listed with its test. |
| Grants | Worker role: `INSERT` on `entities`, `aliases`, `edges`, `events`, `event_participants`, `themes`, `entity_state`, and `UPDATE` on `entity_state`, in `WORKER_TABLE_PRIVILEGES` (`grants.py:57-71`), which the startup check, fixtures and role script read. Tenant role: nothing new (it reads memory already). Purge: nothing. (B): `EXECUTE` on one function in `TENANT_FUNCTION_PRIVILEGES`. |
| Two-readers tests | A's job never reads or writes B's rows (hydration and publish bound to the claimed owner); B's routes answer A's book as missing; A's correction during B's job changes nothing of B's. |
| Parity tests | New: the same fixture chapters, a stand-in extractor and reviewer, through local `IngestWorker` and through the hosted handler, compared on logical rows and on `score_recap`, `same_person_names`, `completion_frontier` and the view payloads at every bookmark (the method of `tests/parity/test_companion_memory_parity.py`). |

### 8. Embeddings and search

Hosted extraction runs with the **stub embedder**, as measured and as the parity corpus does, and
publishes **no passages and no vectors**. Why: no hosted reader feature reads them (the chat reads the
read text, ADR 0041 section 4; hosted search uses `book_search_documents`); storing chunk text and
vectors adds copies of the book to Postgres, to export and to erasure; and the real embedder's only
effect on extraction is the semantic merge of names (`pipeline.py:90-93`, `worker.py:695-696`), which
was not part of the measured run. The extraction client is built with `embed_provider="stub"`
explicitly, because a plain OpenAI key on the default endpoint turns real embeddings on by itself
(`llm/client.py:163-172`). Cost if reconsidered: the app's own upper bound (one token per byte,
`limits.py:271-279`) is about 0.69 million tokens for this novel, about $0.014 at `text-embedding-3-small`'s
$0.02 per million (`limits.py:33`). Because hosted stores no vectors, turning the real embedder on later
changes only how later chapters merge names; but it changes the pin (fact 5), so it would need a
one-time rewrite of each book's pin, which is why it is decided now. The pin lives on the receipts
(`ingested_chapters.extractor_model`, written by the publish) and the platform's fixed stub identity;
no migration.

### 9. The screens

Hosted mode serves, at the local paths the app already calls (`frontend/src/api.ts:939-950,967-970`):
`GET /api/books/{id}/graph`, `GET /api/books/{id}/character/{entity_id}`, `GET /api/books/{id}/notes`
and `GET /api/books/{id}/ingest`. Their bodies are the local ones: `_coalesce_honorific_characters`,
`_character_card`, `_visible_cast` and the route bodies (`api/views.py:201-254,290-308,367-420,1365-1412`) move
verbatim into `app/memory_views.py`, which both modes call over a view. The clamp is the local one
(`api/views.py:181-195`) with the hosted manifest and `finished_through`. `HostedBookStore`'s snapshot
gains edges and entity state with the parity adapter's predicates (`tests/parity/_adapters.py:871-1076`:
`relationships`, `bio`, `current_state`), which the cast and person cards need and the checks did not.
`HostedBookSource` gains the store cross-check it lacks (open items, lines 161 and 170). For a book
that matches a library edition, the same routes read through the door (section 3); for a book with no
memory at all (per-reader memory off), they answer `404` with the detail "no story memory for this
book", which the panels already show as their "I don't keep … yet" lines (any 404, `StoryMemory.tsx:49-50`).

Turned on by this: the cast (`PeoplePanel`), the person card, the story so far (`StoryPanel`, from
`/notes`), "Who was that?" (it reads `/graph` first, `AskTheBook.tsx:660-663`), and the name groups the
chat's checks use. Stays off: `/catch-me-up`, the generated recap, which calls models on the request,
reads raw chapter text (`api/views.py:1647`) and caches in process (`:1509`); it keeps answering 404,
which the app already handles where it asks (not verified for `MothMoments.tsx:134`). The frontend
changes: hosted entity ids are UUIDs, so `entity_id` becomes `number | string` (`api.ts:133,240-247,271`,
the `character` call at `:939`) and `PersonAvatar` picks its colour from a hash of the id
(`StoryMemory.tsx:10-12`); the "waiting" line of section 2. The "absent" lines stay for a server with
story memory off.

### 10. The parts

Paths are under `backend/` unless they start with `frontend/` or `docs/`. Migrations are numbered from
**0025**: `0024_erase_a_removed_book.sql` is about to merge (PR #102). If it does not, the first of these
to merge renames to the next free number (ADR 0041 section 3's rule). Every part: tests
that fail without it, the two-readers test where it touches owner data, stand-in models only, `ruff
check app tests scripts`, and the Postgres suite locally (stacked pull requests get no CI).

**Part 1 — The extraction, moved behaviour-free.** Changes `app/ingest/worker.py`; creates
`app/ingest/chapter_extraction.py`. Proves: the whole local suite unchanged, local extraction tests
untouched. Waits for nothing. No migration. Small.

**Part 2 — Working copy and publish.** Creates `app/hosted/companion/story_memory.py` (hydrate, run one
chapter, read back, publish, settle) and its tests; changes `app/hosted/jobs.py`
(`commit_chapter_memory`), `app/hosted/tenant/grants.py` (worker), `app/hosted/companion/store.py`
(`cost_reservation_ids`, `finalize_ingest`, the chapter-admitting catalog). Proves: parity of section 7
on the fixture corpus; a crash between publish and settle settles once; a publish after a correction is
refused and redone; a second publish of a chapter changes nothing; no raw text or passage reaches
Postgres; two owners. Waits for 1. No migration. Large.
- Built on 17 November 2026 in PR #106: as designed, with no change to shared code (`chapter_extraction.py`, `pipeline.py`, the review, `spend.py` run unchanged), except (1) the publish runs inside the working copy's own commit, after `ingest_chapter` has written the chapter there and before that is committed, not after the run: the shared loop settles the chapter's costs (`finalize_ingest`) straight after its commit and handles a changed roster only around `ingest_chapter`, so publishing inside the commit keeps "publish, then settle", and a refused publish rolls the copy back, rebuilds it and reaches the unchanged loop as its own `StalePreparedRoster`, which prepares the chapter again without paying for the extraction again; (2) "a row recorded after the copy was taken" is checked as "the rows the copy was built from no longer read the same" (one statement, `story_memory.COPY_SQL`, read by the tenant role to build the copy and by the worker inside the publish, compared by digest), because `recorded_at` is a transaction's start time and a correction that began before the copy and committed after it would carry an earlier time; and the correction route's `replace_memory_entity` now takes the book's companion lock first (the lock removing a book and the publish take), so a correction lands wholly before the publish's check or after its commit (one statement added in `tenant/repository.py`); (3) the copy holds the live chapters that have a receipt (with their text from `HostedBookSource`, checked against the published SHA-256), entities, aliases and states, and the pin; not `entity_corrections`, which nothing in extraction reads (a correction's new person, names and state are copied, and its rows are what the check above sees); the pin is the extraction model all receipts carry (they must agree) and the stub embedder's own identity, and `ingested_chapters.extractor_model` is written with the pin; (4) the worker role also needs `SELECT` on `entities`, `aliases` and `entity_state` (the publish's check reads them, and `UPDATE` on `entity_state` reads its `WHERE`), beside the section 7 list; the tenant role gains nothing; (5) the working copy is an in-memory SQLite `MemoryDB` (`:memory:` works), so no chapter text touches the worker's disk; (6) a run passes the shared loop the chapter before the one asked for as well, so the loop finalizes it when it finds its receipt: a crash after a publish is settled by the next run; passing the whole book would make every run touch every earlier chapter; (7) a run that died mid-chapter leaves that chapter's calls reserved, which the shared loop treats as "outstanding, reconcile first" for good, so before extracting a chapter that has no receipt the run settles its open reservations of the extraction purposes, at their noted (or reserved) size, as `extraction-abandoned`; (8) Part 3 calls `remember_chapter(claim, repository, *, hold, tenant_dsn, settings, client, storage, sources, ordinal)` with the lease's `hold` (not the lease, so `companion/jobs.py` can import it without a cycle); it refuses a client that does not embed with the stub, refuses a chapter whose earlier chapters are not all published (`ChapterOutOfOrder`), returns whether this run published, and admits the chapter whole through `ChapterCatalog`, a `ReplyCatalog`; (9) the publish is audited as `memory.publish`; (10) Postgres refuses an edge from a person to themselves (`edges` checks `src_entity_id <> dst_entity_id`), which the local store accepts: such a chapter fails to publish, every time (open items).

**Part 3 — Jobs, triggers, money, rejections.** Creates `app/hosted/schema/0025_story_memory_jobs.sql`
(kind `story_memory` in `jobs_kind_ck`, the pending index, the companion shape check, failure code
`grounding_rejected` in both checks of `0008`). Changes `app/hosted/jobs.py` (`COMPANION_KINDS`, writer
pause), `app/hosted/companion/jobs.py` (the kind, the queue), `app/hosted/companion/routes.py`
(triggers), `app/hosted/tenant/api.py` (no `ingest_book` job), `app/config.py`
(`HOSTED_STORY_MEMORY`), `endpoints.json`, `tests/hosted/test_tenant.py`. Proves: one waiting and one
running per owner and book; the target never passes the read map even when the counter does; a capped
reader's chapter waits whole to the next day and a chapter once admitted finishes; two rejections
publish an unremembered chapter and extraction continues, never looser than local on the spoiler
probes; lost lease, cancel and restart; two owners. Waits for 2. Migration 0025. Large.
- Built on 17 November 2026 in PR #107: as designed, with no change to shared code (`chapter_extraction.py`, the review, `spend.py` and the readers of memory run unchanged), except (1) the marker is `chapters.kind = 'unremembered'`; nothing reads `kind` yet (`HostedBookStore`, the checks and the views do not), so the story view's line is left to the screens; the unremembered chapter is the existing empty extraction, made by a subclass of `ChapterExtraction` in `story_memory.py` whose `_complete_extraction` returns `_empty_extraction`, so it calls no model and everything after it runs unchanged; (2) two failed runs of a chapter count when either code says the chapter itself failed: `grounding_rejected` (the review's rejection after its repair) or the existing `invalid_model_output` for the other failures that repeat on every run and were otherwise paid for again on every enqueue: an extraction malformed past its retry, a review verdict malformed past its repair or a review that cannot cover the chapter (`ExtractionSafetyError` with no cause or a value error as its cause), a call past a per-call ceiling (`CostCeilingExceeded` with no cause, not a cap), and a chapter that ties a person to themselves, which the publish now refuses before writing (`SelfRelation`) rather than meeting the `edges` check, because dropping the edge would publish less than the review cleared; (3) the run names its chapter in the job's `payload_metadata` at its start, through a new fenced worker method `begin_story_memory_chapter`, which in the same transaction counts the book's earlier failed runs of that chapter (same owner, book and incarnation); (4) a run that fails on its chapter asks for the next run itself (one more waiting job, through the tenant role) before it is failed, since a failed job is not retried and the rule needs a next run; (5) only the writer's `ProviderUnavailable(kind="authentication")` (401 or 403) pauses, as `unauthorized`, for the notes' five minutes, under the extraction client's own provider name; an exhausted OpenAI account answers 429, which the client reports as `rate_limit`, so it is retried, not paused; an outage of a review call reaches the job wrapped in `ExtractionSafetyError` and is recognised by its cause; (6) `HOSTED_STORY_MEMORY` is refused in local mode and requires `READER_CONVERSATION_JEV` (the chat's writer and Jev keys and explicit caps, which the worker's clients already need); with `HOSTED_READER_STORY_MEMORY` off the dispatcher still claims the kind (for part 5) but a run for a reader's book does nothing; (7) the extraction client is `build_extraction_client` (`app/llm/factory.py`): the writer's account only, the provider's default model on both tiers whatever `LLM_*_MODEL` say, and the stub embedder set explicitly; (8) no route was added; the inventory gains the background resource `story-memory` (version 22); (9) the reader-key path: only the `ingest_book` job at upload is retired (none is made with story memory on, whatever `HOSTED_READER_KEYS` says); its kind, `commit_chapter`, `reserve_spend`, `resolve_credential` and the job routes stay for old rows; (10) a retried failure (a cap, an outage, anything transient) that uses up a job's three attempts ends it `failed`, as for notes; the book then waits for the next finished chapter or "I've read this" to ask again; (11) the deployment still runs one worker by default; `--scale worker=2` runs the two the design names (compose comment only).

**Part 4 — The screens.** Creates `app/memory_views.py`; changes `app/api/views.py` (the move),
`app/hosted/companion/memory.py` (edges, entity state), `app/hosted/companion/source.py` (store
cross-check), `app/hosted/companion/routes.py` (four routes), `endpoints.json`, `test_tenant.py`,
`frontend/src/api.ts`, `frontend/src/reader/StoryMemory.tsx`. Proves: the payloads equal locally and
hosted on the corpus at every bookmark; a future person's card answers 404 in both; two readers; the
pending-deletion walk passes; frontend tests with UUID ids. Waits for nothing to build (seeded rows);
merges after 2. No migration. Medium.
- Built on 17 November 2026 in PR #105: as designed, except (1) `app/memory_views.py` also holds the clamp (`clamp(requested, place, durable)`, the two local lines) and the `/ingest` body from `app/api/ingest.py`; `app/api/views.py` imports the three helpers back, since local tests import them from there; (2) the hosted place is `min(counter, finished_through)` from `read_snapshot` (`_place` in `routes.py`), so a device that raised the counter moves nothing; part 5 changes only what the counter is; (3) the local views run through `ContentStore`, which leaves out chapters that are not part of the book proper (`excluded_ordinals`); the hosted snapshot's view takes the same exclusion through `ContentSnapshot`, which the ADR did not name; (4) the hosted `bio` has no `appears_in_events` (no participants are loaded; no screen shows them); (5) `HostedBookSource` takes the reader's store as an optional fourth argument, and only the four screens give it one: the chat's seeded tests use keys that are not the book's, so the chat keeps its source unchecked until parts 2 and 3 (open items); (6) a person's id in the path is a string, and a malformed one is the same 404 as an unknown one; (7) `/ingest` reads the book's active `story_memory` jobs: `waiting` when the only one was moved to the next UTC midnight by the claim, `running` otherwise, `idle` with none; a book with no memory rows and no job answers 404 "no story memory for this book" on all four; the status was tested with the job kind added in the test database, standing in for part 3's migration; (8) the panels ask `/ingest` once when what they show is behind the reader, and only then can say "I’m a little behind you. I’ll catch up on the rest tomorrow."; (9) the four routes are inventoried as `memory` reads; version 22.

**Part 5 — Library memory through the door (decided).** Creates
`app/hosted/schema/0026_library_story_memory.sql`: `reading_state.memory_through` (integer, at least 0,
default 0); a one-row service table `library_account (account_id UUID PRIMARY KEY REFERENCES users(id))`
added to the service-table list of `tests/hosted/test_migrations.py`; the `SECURITY DEFINER` function
`library_story_memory(book uuid)` of section 3, with `EXECUTE` revoked from `PUBLIC`. Creates the
operator command `app/hosted/library_memory.py` (create the account, add and build an edition, rebuild
one). Changes `app/hosted/companion/memory.py` (load through the door for a matching book),
`app/hosted/companion/store.py` (`get_state` and `set_bookmark` use `memory_through`; progress from the
door's receipts), `app/hosted/tenant/grants.py` (`EXECUTE`), the correction route, ADR 0017 section 3,
and the privacy text's list. Must prove, with stand-in models:
- for a built book and three readers at chapters 2, 5 and 10, the door's result for each holds no row
  stamped after that reader's place and no `invalid_at` past it, checked on the raw function result,
  through the hosted adapter on the RLS-enforced tenant role, and through a superuser connection
  calling the function with that reader's owner set;
- a reader cannot make the door use any place but their own: no argument exists, a device's position
  report that raises `bookmark` does not move `memory_through`, and a `memory_through` written past every
  chapter the reader has a read range in is capped by the backstop;
- moving the place back ("I haven't read this") shrinks the result; reading on grows it;
- a reader whose book is not a library add, or whose file differs, or who owns no live copy, gets
  nothing from the door (the same answer as for a missing book);
- the full local-versus-hosted parity of the views and of `score_recap`, `same_person_names` and
  `completion_frontier` at every bookmark still holds through the door (the local side being a local
  memory built only to that bookmark);
- the chat's spoiler probes pass with the door in place, including the probe of section 3 for a later
  name the writer remembers (its result decides whether version 2 goes back to the owner);
- progress (`get_state`, `/ingest`, the completion frontier) never passes the reader's place;
- erasing a reader or removing their book leaves the library's rows; corrections are refused;
- two readers of one edition see nothing of each other (positions, conversations, notes).
Waits for 3 and 4. Migration 0026. Medium to large.
- Built on 17 November 2026 in PR #108: as designed, with no change to the modules that decide what a reader is shown, except (1) `set_bookmark` writes `memory_through` and, as before, the older counter, so the reader's position still follows the map (the existing read-map tests read it); `get_state`'s counter is `LEAST(memory_through, bookmark)` once the book's read map has begun, so a device can never raise it and "start over" still brings it down, and before that the older counter, from which the shared code begins the map once (books moved in from local mode carry their progress there); a map begun by a story screen, the conversation or a copy, with no read-map route since, hands the door nothing for that one request; (2) the door rewrites each chapter key from the edition's book id to the reader's (`<book id>:<file>`), or the frontier and the store cross-check would fail; (3) the door also leaves out an alias, tie or state of a person not met by the place, and is a `MATERIALIZED` place plus primary-key lookups: `EXPLAIN` on a 60-chapter edition among 40 other owners, a reader halfway, about 17 ms with 300 people and 37 ms with 3,000 (every table read through its primary key); no index added; (4) a book the door serves has story memory from the first page (the same for every reader of that file), so the panels show the empty cast, not "none kept"; its `/ingest` status is the reader's own (`idle`), not `done`; (5) the door is not behind `HOSTED_STORY_MEMORY`: it hands out only what the operator built, and nothing is built with the setting off; (6) the library account is a suspended user row with no sign-in identity, which every session check refuses; the command's own role (`library`, row security on) holds only `INSERT` on `users` and `SELECT, INSERT` on `library_account`; the add, the queue and the report go through the tenant role bound to the account; the worker gains `SELECT` on `library_account` to tell the account's jobs apart (target: the last chapter, run whatever `HOSTED_READER_STORY_MEMORY` says), and `StoryMemoryQueue` and the job refuse a reader's book the door serves; (7) no rebuild command; (8) three unremembered chapters in a row stop a book with `too_many_unremembered` (both failure checks, migration 0026), checked before a run spends and after each publish; (9) the export now carries each book's cast and story so far at the reader's place, own or through the door (section 7); (10) the corrections route answers 409 with "Mothli’s notes on free-library books are prepared once for everyone, so they can’t be corrected here."; the reader's `GET /memory` routes read their own rows, empty for such a book; (11) the chat's spoiler probes through the door drop exactly what a memory built only to the reader's place drops (in the probes, foreshadowing); a person or name first met after the place is dropped by Jev and the second look, never by the fixed check, which the stand-in probe records; the real-model probe is `scripts/library_later_name_probe.py --run`; (12) one setup line of an existing jobs test now raises both counters, since a device can raise only one.

**Part 6 — Cost proposals of section 5**, each on its own, after the measurements of section 4. Changes
`app/ingest/extraction/validation.py` (shared code: the local suite must stay green). Small each.

**Side by side, and where they collide.**

| Shared file | Parts, in order |
|---|---|
| `app/hosted/jobs.py` | 2, 3 |
| `app/hosted/tenant/grants.py` | 2, 5 |
| `app/hosted/companion/routes.py`, `endpoints.json`, `test_tenant.py` | 3, 4 |
| `app/hosted/companion/memory.py` | 4, 5 |
| `app/hosted/companion/store.py` | 2, 5 |
| `app/config.py` | 3 |
| `app/ingest/worker.py` | 1 |

Part 1 first. Part 4 can be built beside 1 to 3 and merges after 2, rebasing on 3 for `routes.py`.
Part 5's door function and its tests can be written beside 3 and 4 against seeded library rows; it
merges after both. For launch, parts 1 to 5 are needed even if per-reader memory stays off, because the
library editions are built by the same pipeline.

## Consequences

- Hosted readers get the cast, the story so far, "who was that?" and the name groups, from the same code
  as local mode; the spoiler rules exist once. One behaviour-free move in shared code (part 1), and one
  in the views (part 4).
- ADR 0041 section 4's sketch (a persistent working file) is replaced by a working copy rebuilt per
  chapter; ADR 0041 part 10 is replaced by parts 1 to 5 here.
- ADR 0024: hosted extraction publishes through a new fenced method; `commit_chapter` stays for the
  retired path. ADR 0026: no running path waits for a reader's extraction setting.
- Hosted mode differs from local mode in one place: a chapter rejected twice is kept as an
  unremembered chapter and extraction goes on, where local mode stops (and pays again).
- Library titles are built once by the operator and handed out through a database door that returns
  only what is at or before each reader's place; ADR 0017 section 3 is amended for them. Per-reader
  memory (about $7.30 for a novel of this length at terra's list price) is built but off unless the owner
  sets it, with caps to match.
- With no later data handed out, the fixed name check protects a library reader as per-reader memory
  does, not as the unclamped sharing would have; names the writer remembers from a famous novel are
  left to Jev and the second look, as today.
- Extraction runs on terra and the stub embedder, pinned per book from the first chapter; changing
  either later is a paid re-extraction or a pin rewrite.

## What this record is least sure of

1. **The cost of a whole book.** Eight short chapters of one novel; later chapters carry a larger
   roster; caching unmeasured. The $7.30 may be low.
2. **How often terra rejects a chapter.** Zero in eight chapters; luna one in two. The unremembered
   chapter keeps a book going, but a book that loses many chapters would show a story with holes.
3. **Rebuilding the working copy for each chapter.** Correct by construction, but the time and memory
   to hydrate a long book's rows for every chapter were not measured; nor whether `MemoryDB` can live
   in memory.
4. **The library door.** Whether Project Gutenberg's files stay byte-identical long enough to make an
   edition worth building; whether `memory_through` stays in step with the read map in every path (it
   moves the companion's notion of the counter off the device-raised column); and whether Jev and the
   second look catch a later name the writer remembers, which decides whether the bare list of later
   names comes back to the owner.
5. **Paid-API speed.** Two to three minutes a chapter on the subscription; a paid call longer than the
   two-minute lease would let another worker repeat the chapter (paid, published once).
