# ADR 0041 — The companion in hosted mode

**Status:** Proposed (2026-11-16). Accepted when merged. Amends ADR 0017 (sections 3 and 8), ADR
0026 and ADR 0027 where stated below.

File and line references are to `main` at 8856b34 (PR #83). Nothing in this record was run against a
real model, a real database or a server; every claim about behaviour comes from reading the code
cited next to it.

## Context

Local mode (`DEPLOYMENT_MODE=local`, one reader, SQLite) has the whole companion: the read map, the
streamed reply checked sentence by sentence, stored conversations, chapter notes, the home chat,
what Mothli remembers about the reader, and suggestions. Hosted mode (`DEPLOYMENT_MODE=hosted`, many
readers, Postgres, sign-in) has none of it; `docs/product/go-live-map-2026-11-17.md` maps the gap.
Several builders will now port the companion at the same time, one part each. They need one design
so that their pieces fit and nobody writes a second version of the spoiler checks.

Settled by the owner, and taken as given here:

- **The owner pays for the AI**, with a spending cap per reader and per day. Readers do not bring
  keys. This replaces, for the companion, ADR 0017 section 8 ("BYOK") and the "server-paid model usage
  is out of the first hosted release" line at the end of ADR 0017.
- **Sign-in through one outside OpenID Connect issuer** offering email, Google and Apple (vendor not
  chosen). The existing code already assumes one issuer (`config.py:122`, ADR 0020).
- **Hosting:** web server, worker and Postgres in containers on the owner's VPS, Cloudflare in front.
- **English only.**
- **Spoiler safety is not negotiable.** Nothing past what a reader has read of a book may reach them;
  for a book they have not read, only its reviewed catalogue line; every sentence shown is checked;
  nothing in the port may weaken a check; no reader may ever see anything of another reader's.

Not decided yet, and designed so either answer works (costs are given where each comes up): the cap
amounts; limits on free use; what happens to a reader's data when they delete a book or their
account; whether two readers with the same book may share Mothli's chapter notes.

## Decision

### 1. The seam: the companion modules run unchanged against owner-bound adapters

**What the modules call.** Grepped from `read_map.py`, `stream_conversation.py`, `chapter_notes.py`,
`recollections.py`, `home_conversation.py`, `home_suggestions.py`, `reader_place.py` and the helpers
they call on those objects (`read_frontier.source_state`, `cost.budgeted_completion`,
`llm.jev_budget.budgeted_jev`, `eval.spoiler_gate.score_recap`, `known_names.same_person_names`).
`home_suggestions.py` and `reader_place.py` call nothing on any of the four objects (they read
`settings.classics_*` only, and the shipped catalogue files).

**On `catalog`** (methods in `backend/app/catalog/catalog.py`):

| Method (line) | Called from | Returns |
|---|---|---|
| `get_book(book_id)` (148) | `read_frontier.source_state` (`read_frontier.py:50`), so every module; `Recollector._conversation` (`recollections.py:445`) | book dict (`book_id`, `title`, `author`, `incarnation`, `file_hash`, ...) or `None` |
| `get_state(book_id)` (218) | `source_state` (`read_frontier.py:50`) | `{bookmark, cfi, ingest_progress, position_epoch, updated_at}` or `None` |
| `list_books()` (176) | `recollections.py:152,227,244,441`; `home_conversation.py:145` | all book dicts, oldest first |
| `set_bookmark(book_id, n)` (253) | `read_map.sync_counter` (`read_map.py:108`) | nothing; may lower the counter |
| `get_conversation_frontier(book_id)` (265) | `read_map._started` (`read_map.py:37`), `chapter_notes.build_missing` (`chapter_notes.py:193`), `read_frontier.current_snapshot`/`requested_snapshot` | `{position_epoch, atom, source_offset, source_hash, cfi}` or `None` |
| `add_read_range(book_id, *, incarnation, atom, start, end, source)` (361) | `read_map.py:39,154,172` | nothing; merges touching ranges |
| `get_read_map(book_id, *, incarnation)` (380) | `read_map.py:53,158,182,203`; `recollections.read_now` (`:151`) | `{atom: [(start, end), ...]}` |
| `add_skimmed_range(...)` (391), `get_skimmed(...)` (409) | `read_map.py:151,94` | nothing; `{atom: [(start, end)]}` |
| `clear_read_map(book_id, *, atom=None)` (420) | `read_map.mark_chapters` (`read_map.py:175`) | nothing |
| `set_read_position(...)` (434), `get_read_position(...)` (442) | `read_map.py:34,41,54,157` | nothing; `(atom, offset)` or `None` |
| `add_turn(book_id or None, *, incarnation, position_epoch, atom, offset, role, content, reaction, request_id, drew_on)` (316) | `stream_conversation.py:767,770`; `home_conversation.py:449,451` | the new turn id (an increasing integer) |
| `get_turns(book_id or None, *, incarnation, reactions=False, after=0)` (334) | `read_map.visible_turns` (`:264`), `home_conversation.home_turns` (`:176`), `recollections.pending` (`:265`) | turn dicts `{turn_id, position_epoch, atom, source_offset, role, content, reaction, created_at, drew_on}`, oldest first, `turn_id > after` |
| `clear_turns(book_id or None)` (449) | the local routes (`api/views.py:639`, `api/profile.py:104`), `Recollector.clearing` (`recollections.py:490`) | nothing |
| `get_chapter_notes(book_id)` (309), `set_chapter_notes(...)` (300) | `chapter_notes.py:60,196,238` | note dicts with `sentences` as a list |
| `get_recollections()` (458), `keep_recollections(changes, *, cap, cursor)` (468), `touch_recollections(ids)` (513), `delete_recollection(id)` (520), `clear_recollections()` (527), `get_recollection_cursor(book_id or None, *, incarnation)` (545) | `recollections.py:224,232,243,264,458,465,477,505`; the local routes | rows; count stored; nothing; `bool`; nothing; last turn id looked at |
| `reserve_cost(book_id or "home", *, phase, model, input_tokens, output_tokens, usd, max_input_tokens, max_output_tokens, max_usd, chapter_ordinal=None)` (681) | `cost/limits.py:222` (`budgeted_completion`), `llm/jev_budget.py:22` (`budgeted_jev`): every writer and Jev call of every module | reservation id; raises `RuntimeError` at a ceiling |
| `note_reservation_actual(book_id, id, *, input_tokens, output_tokens, usd, model=None)` (760), `settle_cost(book_id, id, *, phase=None)` (780), `discard_cost_reservation(book_id, id)` (806) | the same two wrappers | nothing |

`set_conversation_frontier` is called only by `read_frontier.confirm_anchor`, which no route reaches
while the read map is on (`api/reading.py:44-46`). Hosted mode runs only with the read map on
(section 7), so the hosted adapter does not offer it and returns `None` from
`get_conversation_frontier`: the map is then seeded from the chapter counter
(`read_frontier.snapshot`, `read_frontier.py:76`), exactly as it is locally for a book with no saved
anchor.

**On `store`** (only `store.book(book_id)`, a context manager over the per-book memory,
`memory/store.py:108`):

| Call on the yielded `mem` | Called from | Returns |
|---|---|---|
| `mem.completion_frontier(manifest_atoms)` (`memory/dal.py:592`) | `stream_conversation.py:588,708`, `chapter_notes.py:229`, `home_conversation.py:390` | highest contiguous chapter with a completion receipt |
| `mem.view(bm).characters()`, `.aliases_of(entity_id)` | `stream_conversation.py:589`; `known_names.same_person_names` (`known_names.py:16-25`) | visible character rows; visible alias rows |
| `score_recap(mem, bm, sentence, read_text=...)` (`eval/spoiler_gate/synthesis.py:410`) | `stream_conversation.py:386` (inside `sentence_checks`, so every module's `safe` gate) | `{future_entity_leaks, prolepsis_hits, ...}` |
| inside `score_recap`: `mem._audit_all("entities" / "aliases" / "themes")` and `supplied_facts(mem, bm)` = `view(bm).entities_of_type(t)`, `.aliases_of`, `.chapter_summaries()`, `.timeline()` (`synthesis.py:313-342,423-426`) | as above | all rows (the forbidden-name oracle reads future rows on purpose); visible rows |

The worker's `source_chapters` also opens `store.book` for `assert_matches_store`
(`ingest/worker.py:385-387`, `ingest/manifest.py:123`: `mem.content_language()`,
`mem.view(n).chapters()`).

**On `worker`:** only `worker.source_chapters(book_id)` (`ingest/worker.py:380`), from
`source_state` (`read_frontier.py:70`). It returns the canonical chapter list `{ordinal, key, href,
title, text, ...}` from `segment_for_ingest`.

**On `client`:** the modules use the real `LLMClient` class (`llm/client.py:106`) and nothing an
adapter must imitate: `copy(client)` plus `conversation_draft_effort`, `conversation_review_effort`,
`_oai.with_options` (`stream_conversation.py:323-329`); `complete(...)` through
`cost/limits.py:184`; `billing_model`, `_model_for`, `provider`, `_is_native_openai`,
`on_text_delta` (`stream_conversation.py:450-453`); `client.jev` (a `JevClient`, `llm/jev.py:82`:
`.model`, `.evaluate(state, questions, timeout_seconds=)`) checked at `stream_conversation.py:507`
and `home_conversation.py:272`. **On `settings`:** plain attributes of the same `Settings` class.
The two background objects passed in, `notes_builder` and `recollector`, are used only through
`.ensure(book_id)` (`stream_conversation.py:774,776`).

**The one hole in the seam.** `source_state` does not go through `catalog` or `worker` for the source
itself: it reads the manifest file and hashes the EPUB under `settings.data_dir`
(`read_frontier.py:56-69`). The map (B1, last bullet) missed this.

**Options considered.** (a) Rewrite the companion for hosted mode against
`PostgresTenantRepository`. (b) Run the modules unchanged against owner-bound adapters with the same
method names. (c) Make the modules take one abstract "companion context" object.

**Decision: (b).** The modules stay as they are; hosted mode supplies:

- `HostedCompanionCatalog(dsn, owner_id)` — the `catalog` methods above, synchronous, bound to one
  owner at construction, built per request (or per job). Same names, same argument names, same return
  shapes (ids and incarnations as strings, as locally).
- `HostedBookStore(dsn, owner_id)` — `book(book_id)` yields a read-only snapshot offering exactly the
  `mem` calls above (section 4).
- `HostedBookSource(storage, catalog)` — `source_chapters(book_id)` and the new
  `verified_source(book_id, book)` below.
- The platform's own `LLMClient` with `.jev`, built once per process exactly as
  `deps.build_state` builds the local one (`deps.py:21-34`).
- `notes_builder` and `recollector` stand-ins whose `.ensure(book_id)` inserts a durable job
  (section 5).

Four small, behaviour-free changes to shared code are allowed, and no others:

1. `read_frontier.source_state` asks the worker for the verified source: the lines
   `read_frontier.py:56-70` move verbatim into a new `IngestWorker.verified_source(book_id, book)`
   returning `(manifest, chapters)`. Locally nothing changes.
2. `stream_conversation.py:739-743` and the matching `home_conversation.py` handler emit a distinct
   error code when the reader's allowance is reached (section 6).
3. `recollections.listed` (`recollections.py:247`) returns `r.get("public_id", r["recollection_id"])`
   as `id`, so hosted URLs carry a UUID (section 3).
4. The construction of the platform clients in `deps.build_state` (`deps.py:21-34`) moves into one
   function both modes call.

Why: every spoiler rule lives in these modules and the helpers they call; a rewrite would be a
second copy of all of it, which is the thing the owner's rule forbids. ADR 0017 section 5 already
requires "the same bookmark-bounded domain operations" from the Postgres side.

**How the owner boundary stays structural.** ADR 0021 requires every public repository method to
take `owner_id`, checked by a static test (`tests/hosted/test_tenant.py` near line 20). The adapters
cannot follow that literally without changing the module call sites, so the rule becomes: an adapter
class takes a required `OwnerId` in its constructor, stores it privately, exposes no way to change
it, and no method accepts any owner-like argument. Part 1 extends the static test to check exactly
that. Every adapter query still carries `owner_id = %s` and runs in a transaction that sets
`app.owner_id` (section 2).

### 2. Sync and async: a synchronous owner-bound adapter, one short transaction per call

**Facts.** The companion is synchronous and threaded: `write_and_gate` runs the writer on a thread
and the checks on `ThreadPoolExecutor(max_workers=4)` (`stream_conversation.py:482-484`); notes and
recollections run on their own executors (`chapter_notes.py:150`). The hosted repository is async
psycopg and opens **a new connection for every transaction** (`tenant/repository.py:45-54`); so does
the worker repository, synchronously (`hosted/jobs.py:99-100`). There is no connection pool anywhere
in hosted mode today. Locally, the streamed route is a plain `def` route returning a
`StreamingResponse` over a synchronous generator (`api/views.py:574-599`), so FastAPI already runs
the companion in its thread pool.

**Options.** (a) A synchronous adapter in the style of `PostgresWorkerRepository`: each method opens a
connection, opens a transaction, runs `SELECT set_config('app.owner_id', ..., true)`, runs its
queries with explicit owner predicates, commits and closes. (b) A bridge from the worker threads to
the running event loop (`asyncio.run_coroutine_threadsafe`) reusing the async repository. (c) A sync
connection pool (`psycopg_pool`, not a dependency today, `pyproject.toml:11-24`).

**Decision: (a)** for now, with (c) as a later drop-in. Why: it is the existing convention
(`jobs.py:99`, ADR 0021 "the connection closes before returning"); it is safe from any thread
because nothing is shared between calls; `set_config(..., true)` is transaction-local, so row-level
security is set on every transaction by construction. (b) would make the per-sentence checks wait on
the event loop that is also serving every other reader, and a blocked loop stalls the whole server.

Consequences that answer the specific worries:

- **A streamed reply never holds a connection while it waits for a model.** Each adapter call is one
  short transaction (milliseconds). A reply makes roughly 2 to 3 transactions per model call
  (reserve, note actual, settle) plus a handful of reads; at the measured 7.3 Jev calls per reply
  (`docs/product/companion-audit-2026-11-14.md:714`) that is about 30 to 50 short transactions.
- **Parallel checks:** the four check threads each open their own short connection; nothing is
  shared. The memory snapshot (section 4) is loaded once per request, not once per sentence.
- **Pool size:** without a pool, open connections ≈ calls per second × call time, small. Part 1 adds
  a process-wide semaphore (setting `HOSTED_COMPANION_DB_CONNECTIONS`, default 20) around the
  adapter's connect, so a burst cannot exceed Postgres `max_connections`. If part 9's load check
  shows connection set-up dominates, swap in a `psycopg_pool.ConnectionPool` behind the same
  `_connect`; the transaction-local owner setting keeps that safe, and a test must prove a returned
  connection carries no owner.
- **Thread budget, not connection budget, is the real limit.** FastAPI's thread pool (40 by default
  in the anyio version FastAPI uses) is held by each open stream between sentences. Part 9 raises the
  limit at startup and/or runs more than one web process; measure before choosing.
- **Per-book serialization.** Local writes are serialized by the per-book request lock
  (`deps.py` `book_lifecycle`) and SQLite. Hosted adapter methods that read-then-write (merging
  ranges, keeping recollections) take `pg_advisory_xact_lock(hashtextextended(owner || ':' || book, 48))`
  inside their transaction, the pattern of `tenant/repository.py:612-614` with a new seed, so two web
  processes agree.
- The hosted routes stay `def` routes using the existing async dependencies `_owner` and
  `_csrf_owner` (`tenant/api.py:250-275`), so session, CSRF and the per-owner request limit apply
  unchanged.

### 3. The tables

Conventions taken from `backend/app/hosted/schema/`: `owner_id UUID NOT NULL`; book-scoped rows
carry `(owner_id, book_id, book_incarnation)` with a composite foreign key to
`books(owner_id, id, incarnation) ON DELETE CASCADE`; the primary key starts with `owner_id`; every
tenant table gets `ENABLE` and `FORCE ROW LEVEL SECURITY` and one policy named
`<table>_owner_isolation` with `USING` and `WITH CHECK (owner_id = app_current_owner_id())`
(`0016_owner_scoped_book_search.sql:2-28`); a comment header names the work. Each new table must also
be added to the tenant role allow-list (`tenant/repository.py:57-85`), the test fixture grants
(`tests/hosted/test_tenant_postgres.py:80-98`) and, for service tables, the service-table list in
`tests/hosted/test_migrations.py:91`. Part 1 moves the allow-list into one data module,
`backend/app/hosted/tenant/grants.py`, read by the startup check, the test fixture and the deployment
role script, so later parts edit one place.

Changes from the local shapes that apply to every table: `incarnation TEXT` becomes
`book_incarnation UUID`; `TEXT` timestamps become `TIMESTAMPTZ`; JSON text becomes `JSONB` with a
type check; integer 0/1 becomes `BOOLEAN`; local "every row in the table" queries become "every row
of this owner". Hosted books are soft-deleted (`books.deleted_at`), so the adapter's `get_book` and
`list_books` return only live books, and every other read is reached through them; that hides a
deleted book's companion rows whatever the deletion policy turns out to be. The tenant role gets
`DELETE` on every companion table so either deletion policy can be built without a grant change.

**`read_ranges`** (local `catalog.sql:152`) — migration 0018:

```sql
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
```

Local has no key; merged ranges never overlap within one `(atom, source)`, so the start offset is
unique. The key prefix serves `get_read_map`. Grants: `SELECT, INSERT, DELETE`.

**`skimmed_ranges`** (`catalog.sql:166`) — 0018. Same as `read_ranges` without `source`; key
`(owner_id, book_id, book_incarnation, atom, start_offset)`. Grants `SELECT, INSERT, DELETE`.

**`read_position`** (`catalog.sql:178`) — 0018. Columns `owner_id, book_id, book_incarnation,
atom INTEGER >= 0, source_offset INTEGER >= 0, updated_at`; key
`(owner_id, book_id, book_incarnation)` (local keys by `book_id` alone and stores the incarnation
beside it; a new incarnation is a new row here, which `get_read_position` already filters for). Grants
`SELECT, INSERT, UPDATE, DELETE`.

**`conversation_turns`** (`catalog.sql:133`) — 0019:

```sql
CREATE TABLE conversation_turns (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  turn_id           BIGINT GENERATED ALWAYS AS IDENTITY,
  book_id           UUID,                 -- NULL: the home conversation
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
-- plus ENABLE/FORCE RLS and conversation_turns_owner_isolation, as above
```

`turn_id` stays an increasing integer, **not** a UUID as the map proposed (B11 part 3): it is the
order of a conversation and the recollection cursor (`get_turns(..., after=cursor)`,
`catalog.py:342`; `recollections.py:264-265`), and no route ever returns or accepts it
(`api/views.py:624-627`). A global identity cannot be reused, so it fixes the local "deleted ids are
reused" problem (`catalog.sql:199-200`). The home conversation is `book_id IS NULL`; the adapter maps
the local incarnation `"home"` (`home_conversation.py:47`) to `NULL` and back. Grants
`SELECT, INSERT, DELETE`.

**`chapter_notes`** (`catalog.sql:116`) — 0021. Columns `owner_id, book_id, book_incarnation,
atom INTEGER > 0, source_hash TEXT CHECK (source_hash ~ '^[0-9a-f]{16}$')` (the format of
`content_hash_of`, `ingest/extraction/chapter_text.py:54-57`), `version TEXT` (1 to 64 characters),
`sentences JSONB CHECK (jsonb_typeof(sentences) = 'array')`, `dropped INTEGER >= 0`, `created_at`.
Key `(owner_id, book_id, book_incarnation, atom)` (local has no incarnation in the key; adding it costs
nothing, since a re-upload is a new book anyway). Grants `SELECT, INSERT, UPDATE, DELETE`.

*If the owner later decides two readers may share notes:* a second, service-scoped table
`shared_chapter_notes (source_hash, version, sentences, dropped, created_at)` keyed by the chapter
text hash, written only by the worker, read by the adapter only for a hash it has just computed from
the reader's own chapter text. Cost of that: it is a content-addressed existence signal (notes
appearing instantly tell a reader that someone else has the book), which ADR 0017 section 3 forbids,
so it needs an amendment there and a privacy-policy line; the saving is about $0.001 of Jev plus one
writer call per chapter (section 6), small next to replies. Recommendation: per reader.

**`recollections`** (`catalog.sql:192`) — 0022:

```sql
CREATE TABLE recollections (
  owner_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recollection_id   BIGINT GENERATED ALWAYS AS IDENTITY,   -- order and model numbering only
  public_id         UUID NOT NULL DEFAULT gen_random_uuid(), -- the only id in a URL
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
-- plus ENABLE/FORCE RLS and recollections_owner_isolation
```

What changes: the local integer id is guessable, reused and used alone in `DELETE
/api/reader/recollections/{id}` (`api/profile.py:126-131`, `catalog.py:520-525`). Here the integer
stays because the module sorts by it (`recollections.py:175,185`) and the adapter uses it, always with
`owner_id`, in `touch_recollections` and `replaces`; the URL carries `public_id`, and a foreign or
missing UUID gives the same `404 {"detail": "unknown recollection"}` (ADR 0021). The 300 cap
(`STORE_CAP`, `recollections.py:43`) and its eviction order (`catalog.py:497-501`) apply per owner,
never across the table (map B9.1). `clear_recollections` deletes this owner's rows only. The
lengths in the checks are generous bounds, not product limits; part 7 confirms them against
`recollections.py`. Grants `SELECT, INSERT, UPDATE, DELETE`.

**`recollection_cursors`** (`catalog.sql:208`) — 0022. Columns `owner_id, conversation TEXT
CHECK (conversation = '' OR conversation ~ '^[0-9a-f-]{36}$'), incarnation TEXT, turn_id BIGINT >= 0,
updated_at`; key `(owner_id, conversation, incarnation)` (the local key with the owner in front; `''`
is the home conversation, as locally). No foreign key, because `''` names no book; the book-deletion
path deletes the book's cursor explicitly, as `Catalog.remove_book` does (`catalog.py:209`). The
adapter's cursor update is a compare-and-set (it stores only if the cursor still has the value read
with the batch), which replaces the local in-process guard (`recollections.py:449-469`) now that two
processes can run a batch. Grants `SELECT, INSERT, UPDATE, DELETE`.

**The two home ledger tables** (`home_cost_ledger`, `home_cost_reservations`, `catalog.sql:86,98`)
get **no new table**. Hosted `cost_ledger` and `cost_reservations` already allow `book_id` NULL
(`0003_jobs_costs_credentials_and_notes.sql:109-166`); the adapter maps the local `HOME_SPEND`
(`catalog.py:35`) to NULL. Migration 0020 adds what money needs (section 6):

```sql
ALTER TABLE owner_limits
  ADD COLUMN max_spend_usd_per_day   NUMERIC(20,10) CHECK (max_spend_usd_per_day IS NULL OR max_spend_usd_per_day >= 0),
  ADD COLUMN max_spend_usd_per_month NUMERIC(20,10) CHECK (max_spend_usd_per_month IS NULL OR max_spend_usd_per_month >= 0),
  ADD COLUMN max_companion_calls_in_flight INTEGER NOT NULL DEFAULT 8
    CHECK (max_companion_calls_in_flight > 0);
ALTER TABLE cost_ledger
  ADD COLUMN purpose TEXT CHECK (purpose IS NULL OR purpose ~ '^[a-z][a-z0-9-]{0,63}$');
ALTER TABLE cost_reservations
  ADD COLUMN purpose TEXT CHECK (purpose IS NULL OR purpose ~ '^[a-z][a-z0-9-]{0,63}$');
-- Service-scoped, content-free: the platform's own spend per UTC day.
CREATE TABLE platform_spend_days (
  day           DATE PRIMARY KEY,
  settled_usd   NUMERIC(20,10) NOT NULL DEFAULT 0 CHECK (settled_usd >= 0),
  reserved_usd  NUMERIC(20,10) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The `phase` check stays closed (`0003:116,143`): the adapter writes writer calls as `synthesis` and
Jev calls as `judge` (the two phases `reserve_provider_call` already accepts,
`tenant/repository.py:605`), and keeps the local phase name (`stream-reply`, `notes-write`,
`stream-sentence-check-failed-reserved`, ...) in `purpose` so cost per feature stays visible.
`platform_spend_days` has no owner and no content, like `provider_capabilities`; it joins the
service-table list in `test_migrations.py:91`. Tenant grants `SELECT, INSERT, UPDATE` on it.

**Jobs and pauses** — 0021 (section 5): `jobs_kind_ck` (`0008_durable_job_leases.sql:3-4`) is
replaced to allow `ingest_book`, `chapter_notes`, `recollect`; a partial unique index allows one
pending companion job per owner, kind and book:

```sql
CREATE UNIQUE INDEX ux_jobs_one_pending_companion_job
  ON jobs (owner_id, kind, COALESCE(book_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE state = 'pending' AND kind IN ('chapter_notes', 'recollect');
-- Service-scoped: the platform's provider account was refused; background work waits.
CREATE TABLE provider_pauses (
  provider      TEXT PRIMARY KEY CHECK (provider IN ('typesafe', 'openai-compatible', 'anthropic')),
  reason        TEXT NOT NULL CHECK (reason IN ('no_credits', 'unauthorized')),
  paused_until  TIMESTAMPTZ NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

0022 widens `jobs_kind_ck` again for `recollect` if part 6 did not already include it (it should).

**Migration numbers, in landing order** (they must be contiguous, `migrations.py:51-54`):

| Number | Part | Contents |
|---|---|---|
| 0018 | 1 | `read_ranges`, `skimmed_ranges`, `read_position` |
| 0019 | 2 | `conversation_turns` |
| 0020 | 3 | `owner_limits` columns, `purpose` columns, `platform_spend_days` |
| 0021 | 6 | `chapter_notes`, job kinds, pending-job index, `provider_pauses` |
| 0022 | 7 | `recollections`, `recollection_cursors` |
| 0023 | 8 | only if account deletion needs a job kind or state |
| 0024 | 10 | only if hosted extraction needs schema (unknown) |
| 0025, 0026 | 11 | free library, history import |

Rule for builders: until the first production deploy, a part that is ready before the part ahead of it
renames its file to the next free number when it merges (a checksum is recorded only when a migration
is applied). After the first deploy, numbers are fixed.

### 4. The per-book story memory

**What the hosted chat needs from it, minimally:** the calls in the `store` table of section 1, all
read-only. For the name check that is: every live entity and alias with its `revealed_at`, live
themes, and through `view(bm)` the visible characters, aliases, entities by type, chapter summaries
and timeline; plus the completion frontier and the content language. Nothing else (no vectors, no
edges for the checks).

**What local mode does when the memory is not built.** Then `ingest_progress` is 0 and
`completion_frontier` is 0, so `durable` is 0 (`stream_conversation.py:587-588,706-708`); no
`same_person_names` groups are made (`:589`); `score_recap` sees no entities, so it finds no
unread-name leaks and the deterministic gate is left with future-tense ("will") hits and event-role
bindings against the passages (`stream_conversation.py:384-391`, `synthesis.py:410-470`). Jev's check
and the second look run on every sentence as always, and the writer is only ever given read text.
This is a supported local configuration, not an accident: `READER_MEMORY_INGEST=false` is allowed
with page context (`config.py:56,160`). The home chat with an empty shelf runs the same way on purpose
(`home_conversation.py:366-375`).

**Options for where it lives.** (a) The Postgres memory tables that already exist per owner
(`chapters`, `ingested_chapters`, `entities`, `aliases`, `themes`, `events`, `chapter_summaries`,
`0002_library_memory_and_vectors.sql`), read through a hosted snapshot object. (b) One SQLite
`memory.db` per book in object storage, downloaded and opened with the unchanged `MemoryDB`.

**Decision: (a).** Why: ADR 0017 section 5 makes Postgres the hosted store for memory, the tenant
role can already read these tables (`tenant/repository.py:67-79`), the parity suite already proves
these tables give the same bookmark-bounded answers as SQLite (`tests/parity`, ADR 0019), and object
storage accepts only EPUBs today (`hosted/storage.py:99-104`). (b) would add a second home for memory
and a storage format change.

`HostedBookStore.book(book_id)` opens one transaction, loads this owner's book's live receipts,
entities, aliases, themes, summaries and events into an immutable in-process snapshot, and yields an
object with `completion_frontier`, `content_language`, `view(bm)` (with `characters`, `aliases_of`,
`entities_of_type`, `chapter_summaries`, `timeline`, `chapters`) and `_audit_all(table)` for the
three tables `score_recap` reads. The bookmark filter in `view(bm)` uses the same predicates as the
parity adapter (`tests/parity/_adapters.py:818-1060`): live receipt, live chapter, `revealed_at <= bm`,
valid time. One snapshot per request: the four check threads share it, there is no per-book lock to
take (the local one exists because the SQLite handle is single-owner, `memory/store.py:108-123`), and
nothing is cached across requests.

**What builds it.** The hosted extraction job (`ingest_book`), which does not exist yet (map B5).
That is part 10, the largest unknown in this design. Sketch, to be confirmed by its own short ADR:
the worker runs the unchanged local extraction for one book against a working SQLite memory file on
the worker's own volume (so entity resolution keeps its running roster), and after each chapter
publishes that chapter's rows to Postgres in one receipt-last transaction (extending
`PostgresWorkerRepository.commit_chapter`, `jobs.py:674`, which today writes only the chapter,
summary, cost and receipt, not entities or aliases). The local-archive importer
(`hosted/local_migration.py:241`) already maps every memory table to its Postgres row and is the place
to take that mapping from.

**A book whose memory is not built yet** gets exactly the local behaviour above: hosted
`get_state` reports `ingest_progress` as the count of contiguous live receipts (0 before extraction),
the snapshot is empty, `durable` is 0. That is as strict as local mode in the same state and never
looser, because it is the same code on the same inputs. Part 4 must prove it with a parity test (an
empty local memory and an empty hosted snapshot give the same `score_recap` result for the same
sentences), and part 5 must run the spoiler probes of the local stream tests with an empty memory.

Honest limit: in steady state a local reader with extraction on has a populated name list and a
hosted reader does not until part 10 lands. What that list adds over Jev, given extraction only runs up
to the chapter counter (`api/reading.py:146,180`), has not been measured; it matters where the read
text and the counter differ (skipped or skimmed chapters) and for late aliases.

### 5. Background work for many readers: durable jobs on the existing table and worker

**What a job is.** A row in the existing `jobs` table (`0003:51-81`) with a closed `kind`:
`ingest_book` (exists), `chapter_notes` (one book), `recollect` (one conversation: a book, or
`book_id` NULL for the home conversation). The payload holds no text, no key and no credential id
(`0008:24-29` already forbids credential-shaped keys); `credential_id` is NULL because the platform
pays. The handler module `backend/app/hosted/companion/jobs.py` dispatches on `kind`:

- `chapter_notes` → `chapter_notes.build_missing(book_id, catalog, store, settings, client, worker)`
  (`chapter_notes.py:183`), unchanged, with the hosted adapters and the platform client.
- `recollect` → the body of `Recollector.catch_up` (`recollections.py:448-469`) re-expressed in the
  handler around the unchanged `pending` and `distil` functions, with the adapter's compare-and-set
  cursor (section 3) in place of the in-process guard. About twenty lines; no spoiler rule is in
  them (those are in `pending`, `usable` and `distil`).

**Enqueueing.** The `notes_builder` and `recollector` objects handed to the stream (section 1) insert
a job with `INSERT ... ON CONFLICT DO NOTHING` against the pending-job index; `ensure` while a job is
already pending is a no-op, as locally (`chapter_notes.py:152-161`). The routes that call `ensure`
locally (`api/reading.py:157,175,251`) do the same in hosted.

**Not done twice.** The claim query (`jobs.py:281`) skips a pending companion job while another job of
the same owner, kind and book is leased or running; so at most one runs and at most one waits. Both
handlers are idempotent anyway: notes are written only where missing for the current text hash and
version (`chapter_notes.py:196`), and a recollection batch is stored only if the cursor has not moved
since it was read. A lost lease (ADR 0024) can at worst repeat a model call, never store twice.

**Under row-level security.** The worker role is BYPASSRLS because it schedules across owners
(ADR 0024; `jobs.py:122-135`). The companion handlers do not use it: after the claim, the handler
builds `HostedCompanionCatalog(HOSTED_TENANT_DSN, claim.owner_id)` and does all owner work through the
RLS-enforced tenant role, so both barriers hold in the background as on the request path. The worker
process therefore needs `HOSTED_TENANT_DSN` as well as `HOSTED_WORKER_DSN`.

**Fairness.** Kept simple for one VPS and one or two worker processes: the existing claim already
refuses to exceed an owner's concurrency (ADR 0027, "Worker claim selection"); companion jobs get
priority above `ingest_book` (they are short), and a reader at their daily cap is skipped by the claim
and their jobs pushed to the next UTC day (`run_after`). No round robin across owners is built until
measurements show one reader starving others.

**"Pause after the provider refuses the account."** Locally one process-wide five-minute pause
follows Jev's `no_credits` or `unauthorized` (`chapter_notes.py:144-145,166-170`). With the platform
paying, the refused account is the platform's, so the pause is platform-wide by design: a handler that
sees those reasons writes `provider_pauses` (section 3) with `paused_until = now() + 5 minutes` and
fails the job as retryable; the claim skips companion kinds while a pause is in force; the web process
reads the same row and skips `ensure`. Replies still try (as locally) and fail with the existing 503
line. The pause is also the operator's alert (section 10).

**Restarts.** Nothing is held in memory: queued work is `pending` rows; a running job's lease expires
and it returns to `pending` (ADR 0024); the pause is a row. One local behaviour is dropped:
`Recollector.clearing` distils a cleared conversation's last messages from memory after deleting them
(`recollections.py:481-499`). Hosted clearing just deletes; up to `BATCH_AFTER - 1` (5) messages said
since the last batch never become notes. Keeping them would mean storing cleared text for the job to
read, which is worse.

**Quotas.** The upload admission counts active jobs against `max_active_jobs`
(`tenant/repository.py:1841,1868`); it must count `ingest_book` only, or a busy companion would block
uploads. `GET /api/jobs` keeps showing `ingest_book` jobs only.

### 6. Money: platform keys, one ledger per reader, caps per day and month

**Where settings come from.** The same environment settings local mode reads, built into clients the
same way: writer `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`, `LLM_LARGE_MODEL`,
`LLM_CHEAP_MODEL` (`config.py:24-33`, `deps.py:21`); Jev `JEV_ENABLED`, `TYPESAFE_API_KEY`,
`JEV_MODEL`, `JEV_TIMEOUT_SECONDS` (`config.py:36-42`, `deps.py:22-34`). In hosted they are secrets
of the container environment only, never in the database or a job. New settings:
`HOSTED_READER_USD_PER_DAY`, `HOSTED_READER_USD_PER_MONTH` (defaults for readers whose
`owner_limits` columns are NULL), `HOSTED_PLATFORM_USD_PER_DAY`, `HOSTED_READER_KEYS` (default false),
`HOSTED_COMPANION_DB_CONNECTIONS`.

**Providers.** The writer client supports `openai-compatible`, `anthropic`, `codex-local` and `stub`
(`llm/client.py:44-49`, detection at `:96`); hosted allows the first two only. Jev is one
`JevClient` against `https://api.typesafe.ai` (`llm/jev.py:82-88`), billed per call through
`budgeted_jev` (`llm/jev_budget.py:13`).

**The ledger.** The adapter's `reserve_cost`, `note_reservation_actual`, `settle_cost` and
`discard_cost_reservation` write the hosted `cost_reservations` and `cost_ledger` (section 3). In one
transaction under the owner lock (`pg_advisory_xact_lock(hashtextextended(owner,47))`, the existing
pattern at `tenant/repository.py:612-614`), `reserve_cost` checks, against ledger plus open
reservations:

1. this reader's spend since the start of the UTC day ≤ the day cap;
2. this reader's spend since the start of the UTC month ≤ the month cap;
3. the existing lifetime `max_spend_usd` if set (`0011_owner_limits.sql:13-14`);
4. this reader's open companion reservations < `max_companion_calls_in_flight`;
5. then, in the same transaction, `platform_spend_days` for today (row lock) ≤
   `HOSTED_PLATFORM_USD_PER_DAY`.

Failure raises `ReaderAllowanceReached` (a `RuntimeError`, so `budgeted_completion` and
`budgeted_jev` turn it into `CostCeilingExceeded` unchanged, `cost/limits.py:234-235`,
`jev_budget.py:29-30`) or `PlatformAllowanceReached`. The per-book arguments (`max_input_tokens`,
`max_output_tokens`, `max_usd`) are accepted and ignored: a per-book ceiling makes no sense when the
owner pays (map B9.5) and local mode keeps it. The per-call token ceiling still applies before any
reservation (`cost/limits.py:211-215`). Reservations left open by a crash stay counted until settled,
as ADR 0027 already accepts.

**Why a separate in-flight limit.** The existing `max_provider_concurrency` of 2 (`0011:11-12`) counts
open reservations (`tenant/repository.py:650-663`). One streamed reply has the writer plus up to four
checks in flight at once (`stream_conversation.py:482-484`), so counting companion calls against 2
would refuse almost every reply. The old `/ask` path keeps its limit of 2.

**What the reader sees at the cap.** The stream already ends with
`{"type": "error", "status": 429, ...}` on `CostCeilingExceeded` (`stream_conversation.py:739-743`);
the one shared-code change adds `"code": "allowance_reached"` when the cause is the reader's or the
platform's allowance, and the frontend shows one plain line, for example: "We've talked a lot today —
let's pick this up tomorrow. Your book and your place are right here." Reading, position sync,
highlights and search take no AI and keep working; the read map keeps recording (it calls no model).
Background notes and recollections for a capped reader wait for the next day (section 5).

**How the limits combine against one reader draining the platform.** In order: the request limit per
reader (600 a minute, `0011:8-10`, consumed by `_owner` on every request including each stream); the
per-call token ceiling; the reader's in-flight calls; the reader's day and month dollars; the
platform's day dollars, which bounds the owner's worst day whatever any reader does. Cloudflare rate
limiting on `/api/auth/*` covers the sign-in routes, which have none (map B2).

**The "paste your own key" code** (`/api/credentials`, `/api/provider-settings`,
`frontend/src/shelf/ProviderSettings.tsx`): **keep but hide**. With `HOSTED_READER_KEYS=false` the
routes answer 404 and are listed as unavailable in `endpoints.json`; the frontend already hides what
answers 404. Removing it would delete tested encryption, rotation and validation code (ADRs 0025,
0026) that a later "bring your own key for more" option would want back. The older reader-key reply
paths (`POST /ask`, `/selection-action`, `/chapter-closeout`, `tenant/api.py:937,1055,1162`) are off
with it; selection help and chapter wrap-up return in hosted only when moved onto the platform client,
which is not in this plan. Note for part 10: upload creates `waiting_configuration` jobs when the
reader has no ready key (`tenant/repository.py:1890`, ADR 0026); in platform mode it must create
`pending` jobs.

**Rough cost per active reader per month — an estimate, not a measurement.** Measured: Jev costs
$0.0006 to $0.0012 per streamed reply (`companion-audit-2026-11-14.md:685,714,756,852`) and about
$0.001 per chapter of notes (`:761`, $0.038 for 34 chapters). The writer's cost on a paid API **has
never been measured**: every run so far used a ChatGPT subscription (`codex-local`) for the writer
(`:721`). The only writer figure is the app's own price table applied to measured tokens on an older,
non-streamed path: about $0.0046 per question at about 3,000 input tokens (`:390-394`). Streamed
replies send more (notes, the reader's place, recollections) and allow up to six second looks
(`stream_conversation.py:53`), so assume $0.005 to $0.015 of writer per reply and one writer call of
similar size per chapter of notes. For a reader who sends 100 replies a month (five a day on twenty
days) and reads 60 chapters: replies $0.6 to $1.6, notes $0.4 to $1.0, recollections a few cents;
**about $1 to $3 a month**, three to four times that for a heavy reader at 20 replies a day.
Extraction (part 10) is not included and not measured; the local per-book ceiling is $5
(`config.py:116`). Part 3 must log the real figure from its first week.

### 7. Settings

`config.py:158-185` today refuses in hosted mode: conversation Jev (`:170-172`), and with it streamed
replies (`:164-165`), the read map (`:178-179`), recollections (`:182-183`) and notes (`:176-177` by
default); deferred memory ingest (`:160-161`); `codex-local` (`:184-185`).

What changes:

- Part 1: in hosted mode only, `READER_READ_MAP=true` is allowed with `READER_PAGE_CONTEXT=true` and
  without streamed replies (the read map sends nothing to any model). Local rules unchanged.
- Part 5: `READER_CONVERSATION_JEV` (and so streamed replies) is allowed in hosted when
  `JEV_ENABLED=true`, `TYPESAFE_API_KEY` is set, a platform writer key is set, `READER_PAGE_CONTEXT`
  and `READER_READ_MAP` are on, and `HOSTED_READER_USD_PER_DAY` and `HOSTED_PLATFORM_USD_PER_DAY` are
  set (no unbounded owner-paid AI). Until parts 6 and 7 land, hosted requires
  `READER_STORY_NOTES=false` and `READER_RECOLLECTIONS=false`; each part lifts its own rule.

What must still be refused in hosted mode: `LLM_TRANSPORT=codex-local` (`:184`, it runs the owner's
own subscription CLI); `JEV_WINDOWS_CREDENTIAL=true` (reads a Windows credential store,
`deps.py:28-29`); `ALLOW_STUB=true`; `READER_MEMORY_INGEST=false` (`:160`, stays); the streamed
companion with `READER_READ_MAP=false` (hosted has no `conversation_frontier`, section 1);
`READER_ANSWER_CACHE`, `RECAP_DISK_CACHE` and `READER_PREPARE_ENABLED`, which write shared files
under `DATA_DIR` (`deps.py:45`, map B9.3); and an `OPENAI_BASE_URL` whose origin is not in
`HOSTED_PROVIDER_ALLOWED_ORIGINS` (`config.py:153-155`).

### 8. The frontend

The reader app calls the **same paths in both modes**; hosted mounts the companion routes at the local
paths (`/api/books/{id}/read-map`, `/read-page`, `/read-chapters`, `/read-frontier`, `/ask/stream`,
`/conversation`; `/api/reader/ask/stream`, `/conversation`, `/recollections`, `/profile`), because
`frontend/src/api.ts:682-799` already calls them and falls back on 404. `/read-offer` stays 404 until
history import (part 11). Changes and missing screens:

- `Recollection.id` becomes `number | string` (`api.ts:153`, `forgetRecollection` at `:724`).
- The allowance line for `code: "allowance_reached"` in the book chat and the home chat (beside the
  existing 429 handling, `reader/AskTheBook.tsx:626`).
- A sign-in and welcome screen in place of the bare "Sign in" link (`shelf/Shelf.tsx:523`), offering
  the issuer's email, Google and Apple.
- An account page: sign out, "what Mothli remembers" (`shelf/MothliRemembers.tsx`, already built),
  delete account, export.
- Hide the AI-key screen (`shelf/ProviderSettings.tsx`) when `/api/credentials` answers 404.
- Signed out mid-read (401): back to sign-in and then to the same place.
- Terms and privacy pages (they must name the writer's provider and TypeSafe, map B9.7).

### 9. Testing: what every part must prove before it merges

Every part:

1. **Two readers.** A Postgres test through the hosted HTTP app on a non-BYPASSRLS tenant role (the
   fixture in `tests/hosted/test_tenant_postgres.py:70-110`): A creates the rows; B, using A's real
   book id, gets the same status and body as for a random missing id, B's lists and counts exclude A,
   and B's writes and deletes change nothing of A's (checked in the database afterwards). The same
   adapter called through a superuser connection with RLS bypassed must still be isolated by its own
   predicates (ADR 0021, Verification). For background work: two owners' jobs, one owner's refused
   or capped work never touches the other's.
2. **Inventory.** Every new route is in `backend/app/hosted/tenant/endpoints.json` with the name of
   its two-reader test, and the inventory version in `tests/hosted/test_tenant.py:29` is bumped.
3. **Parity.** `tests/parity` runs one corpus through the real SQLite stack and the real Postgres
   schema and compares results (`tests/parity/test_spoiler_parity.py:45-55`). Each part adds a
   companion parity test: the same sequence of actions through the local `Catalog` (temporary data
   directory) and through the hosted adapters, compared on what the unchanged modules return
   (`read_snapshot` point and map, `visible_turns`, `usable_notes`, `listed`, `score_recap`).
4. **No real models.** Stand-in writer and Jev clients as the local suite uses; spoiler probes (a
   stand-in writer that emits a future name or a "will" sentence) must be dropped in hosted as in local.
5. **Grants.** The startup role check passes with exactly the new grants and fails with one more.
6. `ruff check app tests scripts` from `backend/`, and the whole suite with
   `TEST_POSTGRES_DSN` set (`backend/scripts/test_postgres.sh`, image `pgvector/pgvector:pg16`; on
   Windows the script's `.venv/bin/python` is `.venv/Scripts/python.exe`). Stacked pull requests do
   not get CI, so the builder runs the Postgres suite locally once Docker is on the build machine.

### 10. Deployment (a list, not a design)

- **One image, two commands**: web `uvicorn app.main:create_app --factory` (with `--proxy-headers`
  and `--forwarded-allow-ips` set to the proxy), worker `mothli-hosted-worker` (`pyproject.toml:31`)
  with `HOSTED_JOB_HANDLER` pointing at the part 6 dispatcher. A migration step
  (`python -m app.hosted.migrations`) runs before the web starts.
- **Containers on the VPS**: web, worker, `pgvector/pgvector:pg16` (the CI image), and a small proxy
  (Caddy or nginx) serving `frontend/dist` and passing `/api` to the web, on one host, because the
  cookies are `__Host-` and SameSite=Lax (map B7). The proxy must not buffer `text/event-stream`.
- **Cloudflare**: proxied DNS, TLS "Full (strict)" with an origin certificate, no caching of `/api`
  (responses are already `no-store`), rate limiting on `/api/auth/*`. Cloudflare's 100-second proxy
  read timeout applies until the first byte; streams send headers at once, so they are expected to
  pass, but the slowest measured reply took 69.5 s (`companion-audit:714`): part 9 checks it.
- **Book files**: the storage abstraction supports encrypted filesystem (AES-256-GCM) and S3 with
  server-side encryption, EPUB only (`hosted/storage.py:99-104,120,285`; settings `config.py:136-147`).
  Use the encrypted filesystem on a VPS volume first; Cloudflare R2 through the S3 backend is
  possible, but whether R2 accepts the server-side-encryption header the adapter sends is untested.
- **Database roles** (ADR 0020, map B7): a migration owner; `auth` (narrow, BYPASSRLS, ADR 0020);
  `tenant` (NOBYPASSRLS, exact allow-list from `grants.py`); `worker` (BYPASSRLS, NOINHERIT, exact
  allow-list, `jobs.py:102-160`); `audit`; an operator role for `python -m app.hosted.limits`. One
  script generates the grants from the allow-lists.
- **Backups**: nightly `pg_dump` (or WAL archiving) and the object volume copied off the VPS (R2 is
  fine; the files are already encrypted); the encryption keys (`HOSTED_CREDENTIAL_MASTER_KEY`,
  `HOSTED_STORAGE_FILESYSTEM_KEY`) kept separately and offline, since losing the storage key loses
  every book; one restore drill before the first reader.
- **Health**: add `/api/health/ready` (database reachable, migrations current, role checks passed);
  hosted has only `/live` (`main.py:485-487`). Alert on: worker not claiming, a `provider_pauses` row,
  platform spend over 80% of the day cap.
- **Before the first real reader**: the ADR 0017 launch gate; sign-in issuer chosen and configured;
  caps set; terms and privacy naming the AI providers and TypeSafe; account deletion working; restore
  drill done; session idle time reconsidered for reading (30 minutes, `config.py:132`).

### The parts

Each part names its files. Paths are under `backend/` unless they start with `frontend/` or `docs/`.
New companion code goes in a new package, `app/hosted/companion/`, so the large files
`app/hosted/tenant/api.py` and `app/hosted/tenant/repository.py` are barely touched. **The map's
part 1 (splitting those files) is dropped**: nothing in this design needs it.

Adapter methods live in one file per feature (`store_read_map.py`, `store_turns.py`, `spend.py`,
`store_notes.py`, `store_recollections.py`), combined into `HostedCompanionCatalog` in `store.py`, so
two parts share only that one combining line and the lists below.

**Part 1 — Proof: the read map in hosted mode, through the seam.** *Build first, alone.*
Delivers the smallest end-to-end proof: three small tables, the read-map routes, the unchanged
`read_map.py` running over the hosted adapter, and the two-reader test.
- Changes: `app/read_frontier.py` (`source_state` calls `worker.verified_source`),
  `app/ingest/worker.py` (new `verified_source` holding `read_frontier.py:56-70` verbatim),
  `app/hosted/tenant/repository.py` (allow-list read from `grants.py`), `app/config.py` (hosted read
  map without streaming), `app/main.py` (include the companion router; build `HostedBookSource` in the
  hosted lifespan), `app/hosted/tenant/endpoints.json`, `tests/hosted/test_tenant.py` (inventory
  version; static test for owner-bound adapters), `tests/hosted/test_tenant_postgres.py` (grants from
  `grants.py`).
- Creates: `app/hosted/schema/0018_companion_read_map.sql`; `app/hosted/tenant/grants.py`;
  `app/hosted/companion/__init__.py`, `store.py` (`get_book`, `get_state` with receipt-derived
  `ingest_progress`, `list_books`, `set_bookmark`, `get_conversation_frontier` returning `None`),
  `store_read_map.py` (the seven read-map methods, with the per-book advisory lock),
  `source.py` (`HostedBookSource`: EPUB from object storage, checksum-verified by the storage
  adapter, `segment_for_ingest` and `build_manifest` (`ingest/manifest.py:52`), bounded cache keyed
  by owner, book and incarnation, as ADR 0022 requires), `routes.py` (`GET /read-map`,
  `PUT /read-page`, `PUT /read-chapters`, `GET`/`PUT /read-frontier`); tests
  `tests/hosted/test_companion_read_map_postgres.py`, `tests/parity/test_companion_parity.py`,
  `tests/hosted/test_companion_source.py`.
- Proves: two readers (B gets A's book as missing on all four routes, A's ranges unchanged by B);
  the same page sequence gives the same `read_snapshot` point, map and fractions locally and hosted;
  `HostedBookSource` gives the same chapters (keys, texts) and manifest atoms as the local import of
  the same fixture EPUB (this settles map B12's first open question); the local suite unchanged.
- Waits for: nothing. Migration 0018. Size: medium (the smallest that exercises every layer).
- Built on 17 November 2026 in PR #85: as designed, except (1) `HostedBookSource(storage, catalog, cache)` takes a third argument, the process-wide bounded parse cache the hosted lifespan builds (the lifespan cannot build the source itself, which is per reader); (2) `store.py` also has `source_record(book_id)`, the stored object (as a `SourceObjectRef` carrying the adapter's own owner) and the chapter lengths recorded at upload, which the source checks a fresh parse against, since hosted mode has no manifest file; (3) the owner-bound base class and connection limit live in `companion/__init__.py`; (4) moving the source check made two local tests patch `app.ingest.worker` instead of `app.read_frontier`, and the symlink test now calls `IngestWorker.verified_source` directly (assertions unchanged); (5) config also refuses chapter notes and recollections in hosted mode (part 5's rule, needed once the read map is allowed there); (6) chapter keys are `<book id>:<file>`, so hosted and local keys, and `atom_set_version`, are equal only past the book id.

**Part 2 — Conversations inside a book.** `conversation_turns`; `add_turn`, `get_turns`,
`clear_turns`; `GET`/`DELETE /api/books/{id}/conversation`.
- Creates `0019_companion_conversation_turns.sql`, `app/hosted/companion/store_turns.py`, tests.
  Changes `grants.py`, `store.py` (combining line), `routes.py`, `endpoints.json`, `test_tenant.py`.
- Proves: two readers; turns hidden after "I haven't read this" exactly as locally
  (`visible_turns` parity); turn ids never appear in a response.
- Waits for 1. Migration 0019. Size: small.

**Part 3 — The platform pays: clients, ledger, caps.**
- Changes `app/deps.py` (client construction moved to a shared function, no behaviour change),
  `app/stream_conversation.py` and `app/home_conversation.py` (the `allowance_reached` code only),
  `app/config.py` (new settings, hosted refusals of section 7), `app/main.py` (platform clients in the
  hosted lifespan), `app/hosted/limits.py` (show and set the new columns), `grants.py`, `store.py`,
  `app/hosted/tenant/api.py` (credential and provider-settings routes 404 when
  `HOSTED_READER_KEYS=false`), `endpoints.json`.
- Creates `0020_companion_spend.sql`, `app/llm/factory.py` (the shared construction),
  `app/hosted/companion/spend.py`, tests.
- Proves: concurrent reservations at the day cap admit exactly what fits (as ADR 0027's tests);
  two readers' caps are independent; the platform day cap stops everyone; a capped stream ends with
  `allowance_reached` and stores no turn; reading routes still work at the cap; stand-in clients only.
- Waits for 1. Migration 0020. Size: medium.

**Part 4 — Story memory for the checks (read-only).** `HostedBookStore` and its snapshot (section 4).
- Creates `app/hosted/companion/memory.py`, `tests/parity/test_companion_memory_parity.py`. Changes
  nothing shared.
- Proves: on the existing parity corpus (`tests/parity/fixtures/spoiler_parity.json`) at every
  bookmark, `score_recap`, `same_person_names` and `completion_frontier` give identical results on
  the SQLite `MemoryDB` and the hosted snapshot, for a fixed list of sentences including future names
  and late aliases; and identical results on two empty memories.
- Waits for 1. No migration. Size: medium.

**Part 5 — The streamed chat inside a book.** `POST /api/books/{id}/ask/stream` calling the unchanged
`stream_read_prefix` with the hosted adapters, the platform client and job-enqueuing stand-ins
(no-ops until part 6).
- Changes `app/config.py` (section 7, part 5 rules), `routes.py`, `endpoints.json`, `test_tenant.py`.
  Creates tests.
- Proves: the local stream tests' spoiler probes pass against the hosted adapters with stand-in
  models, with an empty and with a seeded memory; two readers (B cannot stream from A's book, B's
  conversation never shows A's turns); a stored turn carries `drew_on` with B-free content; cost rows
  land on the right owner with `purpose` set.
- Waits for 2, 3, 4. No migration. Size: medium.

**Part 6 — Chapter notes and durable companion jobs.**
- Changes `app/hosted/jobs.py` (claim skips a kind/book already running, skips paused providers and
  capped owners), `app/hosted/worker.py` (also reads `HOSTED_TENANT_DSN`), `app/hosted/tenant/repository.py`
  (upload admission counts `ingest_book` only; `GET /api/jobs` lists `ingest_book` only), `app/config.py`
  (lift the notes rule), `grants.py` (tenant and worker), `store.py`.
- Creates `0021_companion_notes_and_jobs.sql`, `app/hosted/companion/store_notes.py`,
  `app/hosted/companion/jobs.py` (dispatcher, enqueuer), tests.
- Proves: two owners, one with a refused provider: the pause is platform-wide and recorded once;
  a second `ensure` while pending adds no job; a lease lost mid-run does not store notes twice;
  notes parity (`usable_notes` locally and hosted); worker restart resumes from rows alone.
- Waits for 5. Migration 0021. Size: large.

**Part 7 — Home chat, what Mothli remembers, suggestions.**
- Changes `app/recollections.py:247` (the `public_id` line), `frontend/src/api.ts` (`Recollection.id`),
  `app/config.py` (lift the recollections rule), `routes.py`, `grants.py`, `store.py`,
  `app/hosted/companion/jobs.py` (the `recollect` kind), `endpoints.json`, `test_tenant.py`.
- Creates `0022_companion_recollections.sql`, `app/hosted/companion/store_recollections.py`, tests.
  Routes `POST /api/reader/ask/stream`, `GET`/`DELETE /api/reader/conversation`,
  `GET /api/reader/profile`, `GET`/`DELETE /api/reader/recollections`,
  `DELETE /api/reader/recollections/{public_id}`. `stream_home` gets `elsewhere=[]` until history
  import; suggestions show the reviewed catalogue line and no "add to shelf" until part 11.
- Proves: B cannot list, delete or cap-evict A's notes; A's 301st note evicts A's own oldest only;
  `list_books` in the home chat is A's shelf only; recollection parity (`listed`, `recall`).
- Waits for 2, 3, 5, 6. Migration 0022. Size: large.

**Part 8 — Accounts and the reader's screens.**
- Backend: account deletion (revoke sessions, cancel jobs, delete rows and objects, keep the minimum
  audit) and export, in `app/hosted/auth/` and a new router module; migration 0023 only if deletion
  needs a job kind or state. Waits for the deletion decision.
- Frontend: sign-in and welcome, account page, hidden key screen, allowance line, signed-out
  recovery, terms and privacy (`frontend/src/shelf/*`, `frontend/src/pwa/offline.ts`). Can start any
  time with the API mocked.
- Proves: after deletion no row of that owner remains in any table and no object in storage; the
  other reader is untouched. Size: medium.

**Part 9 — Deployment.** Dockerfile, compose file, proxy config, role-and-grant script from
`grants.py`, `/api/health/ready` (`app/main.py`), backup and restore scripts, a short runbook in
`docs/runbooks/`. Creates new files at the repo root and `deploy/`; touches `main.py` only for
readiness. Proves: images build; the Postgres suite passes inside them; a restore drill from a
backup; a stream passes through the proxy (Cloudflare checked once on the VPS). Waits for nothing
(the grant script adopts `grants.py` after part 1). No migration. Size: medium.

**Part 10 — Extraction for hosted books.** The `ingest_book` handler on the platform client
(section 4 sketch); upload creates `pending` jobs in platform mode. Needs its own short ADR first.
Changes `app/hosted/jobs.py` (`commit_chapter` or a sibling), `app/hosted/tenant/repository.py`
(upload job state), `grants.py` (worker). Waits for 4 and 6. Migration 0024 only if needed.
Size: large.

**Part 11 — Free library and history import.** As the map's part 10. Migrations 0025 and 0026.
Waits for 1 and the free-use decision. Size: medium each.

**Order and what can run side by side.**

| Shared file | Parts that change it, in this order |
|---|---|
| `app/config.py` | 1, 3, 5, 6, 7 |
| `app/main.py` | 1, 3, 9 |
| `app/hosted/tenant/grants.py` | 1, 2, 3, 6, 7, 10 |
| `app/hosted/companion/store.py` (combining line) | 1, 2, 3, 6, 7 |
| `app/hosted/companion/routes.py`, `endpoints.json`, `test_tenant.py` | 1, 2, 5, 7, 8, 11 |
| `app/hosted/tenant/repository.py` | 1, 6, 10 |
| `app/hosted/jobs.py`, `app/hosted/worker.py` | 6, 10 |
| `app/stream_conversation.py`, `app/home_conversation.py` | 3 |

- Part 1 first, alone.
- Then side by side: parts 2, 3, 4; part 9 (from the start); part 8's frontend (from the start).
  Parts 2 and 3 share `grants.py` and the `store.py` combining line, so they **merge in sequence**
  (2, then 3, the second rebasing two short conflicts); part 4 shares nothing.
- Part 5 after 2, 3 and 4. Part 6 after 5. Part 7 after 6. Part 10 after 4 and 6. Part 11 after 1.

## Consequences

- The spoiler rules exist once. Hosted mode adds storage adapters, routes and jobs, and four
  behaviour-free changes to shared code (section 1), each covered by the local suite.
- ADR 0017 is amended: the platform pays for the companion (section 8 of 0017 stays for the hidden
  reader-key path); "no shared derived memory" stands unless the owner chooses shared notes, which
  would need an amendment of section 3 of 0017.
- ADR 0021's "every repository method takes `owner_id`" becomes, for the companion adapters, "every
  adapter is constructed with one `OwnerId` and offers no way to change it", with its own static test.
- ADR 0026: in platform mode, waiting for a reader's extraction setting no longer applies (part 10).
- ADR 0027: companion calls get their own in-flight limit; day and month dollar caps join the
  lifetime cap; a platform day cap is added.
- A hosted reader's chat runs on an empty story memory until part 10, with the same checks local mode
  applies to an unbuilt memory. The cast, recap and character views stay absent until then.
- Clearing a conversation in hosted mode drops its last few undistilled messages instead of turning
  them into notes.
- Whoever runs Jev and the writer sees every reader's sentences; the privacy policy must say so.

## What this record is least sure of

1. **Extraction (part 10).** The working-file-plus-publish sketch is plausible but unbuilt; how much
   the populated name list adds over Jev has never been measured; until then a hosted chat is as safe
   as local mode with extraction off, not as local mode with it on.
2. **Capacity.** One connection per transaction and the 40-thread pool were never measured with
   many concurrent streams; nor were Cloudflare and the proxy with streams near 70 seconds.
3. **Money.** The writer's cost on a paid API has never been measured, so the $1 to $3 per active
   reader per month is an estimate; and lowering the chapter counter through `set_bookmark` beside
   the hosted versioned position (`tenant/api.py:1521-1578`) has not been checked across two devices.
   Part 1 must test that interaction.
