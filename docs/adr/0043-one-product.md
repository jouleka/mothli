# ADR 0043 — One product: what goes now that Mothli is hosted

**Status:** Accepted (2026-11-18). The owner agreed to the whole removal list and answered both
questions at the end: readers never pay for the AI with their own AI account ("they can only pay
us"), and nothing is needed from the copy of Mothli on his own computer. So D1 to D4 are removed
too. Nothing had been removed when this record was accepted; each removal is its own small pull
request.

File and line references are to `main` at 9170e06 (PR #110). Every "depends on" below comes from
searching the code (who imports it, which tests use it, which tables and settings it touches, which
screens call it). No test suite, server, database or model was run for this record.

## Context

The owner asked (18 November 2026): "now that we're moving to hosted, shouldn't we delete or remove
things that we don't need anymore? … delete or remove everything that made it just a solo product
for me, for example, for testing purposes. Aside from things we, of course, need still, like the
ChatGPT subscription."

Mothli has two **modes**, two ways the same program can be started:

- **Local mode**: one reader, on one computer, with a small file database (SQLite). This is where
  every spoiler rule was first built, and the only place the ChatGPT subscription works.
- **Hosted mode**: the real product. Many readers, each signed in, a proper database server
  (PostgreSQL), the owner paying for the AI under caps.

Hosted mode does not have its own copy of the companion. It runs the *same* code that local mode
runs, plugged into its own storage. So local mode cannot simply be deleted. What was checked:

- **Hosted story memory is built in a throw-away local-mode database.** Each chapter's people,
  places and events are worked out inside an in-memory SQLite copy, then published to PostgreSQL
  (`backend/app/hosted/companion/story_memory.py:187`, `dal.MemoryDB(":memory:", …)`). It imports the
  local extraction code (`app.ingest.chapter_extraction`, `app.ingest.extraction.*`, `app.memory.dal`,
  lines 40-51).
- **The hosted chat is the local chat code.** `backend/app/hosted/companion/routes.py` imports
  `app.stream_conversation` and `app.ask`; notes and recollections jobs import `app.chapter_notes`.
- **Most tests go through the local stack.** 124 of the 172 test files are outside `tests/hosted`
  and `tests/parity`; they test the spoiler rules on the local stack. 20 of the 38 hosted test files
  also import local-mode code.
- **The parity tests use local mode as the reference.** The 10 files in `tests/parity` run the same
  reading in both modes and require the same answer at every reading position.
- **The ChatGPT subscription runs only in local mode.** `config.py:218-219` refuses
  `LLM_TRANSPORT=codex-local` anywhere else.
- **The server image already carries local mode.** `deploy/Dockerfile:45` copies all of
  `backend/app`; with `DEPLOYMENT_MODE=hosted` none of the local routes are mounted
  (`main.py:607-640`).

### The proposed shape

**Local mode stops being a product and becomes the workshop.** It stays as the place where:
the spoiler rules are tested, hosted mode is compared against (parity), real models are tried on
the subscription, and the free-library story memory could be built. No reader ever uses it. Nothing
is promised to anyone running it.

**What a reader of the hosted product ever touches:** sign-in, the shelf, the free library, adding
and removing a book, the reader (place, read map, highlights, notes, bookmarks, page look), the chat
in a book and the home chat (streamed, every sentence checked), chapter notes, what Mothli remembers,
the cast and story-so-far screens, the account sheet, deletion, and "take a copy of my data". Every
piece below that a hosted reader cannot reach, and that the workshop does not need, is a candidate.

**What goes:** what only a solo user needed and nothing above depends on. Where something is still
used by the workshop or by the tests, it stays, and this record says why.

## Inventory

Verdicts: **keep as harness** (stays, for the workshop or the tests), **remove**, **owner must
decide** (a real choice only the owner can make; the questions are at the end).

### Keep as harness

| # | What it is | Files | What depends on it (found) | Why it stays |
|---|---|---|---|---|
| K1 | Local mode itself: the SQLite story memory, the local routes, the local ingest worker | `backend/app/api/*`, `app/memory`, `app/ingest`, `app/catalog`, `app/deps.py`, the local branch of `app/main.py` | Hosted story memory (`story_memory.py:40-51,187`); the hosted chat (`hosted/companion/routes.py`); 124 test files; 10 parity files | Hosted mode is built on it; deleting it deletes the product and most spoiler tests |
| K2 | The ChatGPT subscription transport | `app/llm/codex_local.py`, `LLM_TRANSPORT` (`config.py:35`) | `llm/client.py`, `llm/factory.py`, `api/health.py`, `cost/limits.py`, `ingest/chapter_extraction.py`, `page_conversation.py` | The owner asked to keep it; it is how real models are tried at no API cost |
| K3 | Stand-in models (`ALLOW_STUB`) | `config.py:97`; stub client in `app/llm` | Most tests; the hosted walk tools outside the repository | Tests and walks run without paying; already refused in hosted (`config.py:251`) |
| K4 | Reading the checker's (Jev's) key from the Windows password store (`JEV_WINDOWS_CREDENTIAL`) | `config.py:40`, `llm/factory.py:30,74` | 2 test files | How the owner's own machine runs Jev locally without the key in a file; already refused in hosted (`config.py:252`) |
| K5 | The older, non-streamed reply in local mode (`POST /api/books/{id}/ask`) and its variants (`READER_CONVERSATION_PIPELINE`, `_ROUTING`, `_PROSE`, `_DRAFT_EFFORT`, `_REVIEW_EFFORT`) | `api/views.py:561`, `page_conversation.py`, `conversation_pipeline.py` | It is the local reply whenever `READER_CONVERSATION_JEV` is off, the default (`config.py:59`); 12 test files call `/ask`, among them spoiler tests such as `test_views.py::test_ask_the_book_withholds_an_uncited_future_answer_without_leaking_it` and `test_page_read_frontier.py::test_forged_larger_boundary_is_withheld_without_a_model_call`; `scripts/companion_eval.py` imports `page_conversation` | The only way to try the chat locally without paying for Jev. Removing it would delete about 30 spoiler tests that first need equals on the streamed path. A later clean-up, not this one (see "Not in this round") |
| K6 | The AI status line and its "check again" (local only) | `frontend/src/ui/AiStatus.tsx`, `GET /api/health/ai`, `POST /api/health/ai/check` (`api/health.py:25,51`) | `main.tsx:139` turns it on only when there is no signed-in session, i.e. local mode; `Shelf.tsx:894` | Shows whether the subscription is connected; useful in the workshop, never seen by a hosted reader |
| K7 | The parity and database test tools | `tests/parity/*`, `backend/scripts/test_postgres.sh`, `deploy/compose.local.yaml` | `test_spoiler_parity.py` checks `test_postgres.sh` is wired in; the compose file is for trying the whole hosted stack on one machine | Needed to prove hosted equals local |
| K8 | Scripts still in use | `scripts/companion_eval.py` (+ `companion_eval_cases.json`), `voice_samples.py`, `library_later_name_probe.py`, `build_classics_catalog.py`, `hosted_roles.py`, `serve_local_jev.py` | `companion_eval` is imported by 6 test files; the later-name probe by 2 (and ADR 0042); `hosted_roles.py` is copied into the server image (`Dockerfile:46`) and used by `compose.yaml` and `restore.sh`; `build_classics_catalog.py` rebuilds the free library's list (`app/classics.py`) | Evaluation, the free library, deployment, and starting the workshop with Jev |
| K9 | Local backups and restore (`python -m app.lifecycle`) and the in-memory recap cache | `app/lifecycle/*`, `app/lifecycle_cache.py` | `DataDirLock` is used by `main.py:44`; `RecapRegistry` by `main.py:551`; tested in `tests/lifecycle/*` and `test_lifecycle_cache.py` | Part of how local mode protects its folder; small. Loses its only scheduled caller if the service scripts go (D4) |
| K10 | The free library's covers and reading-history import, local only today | `api/classics.py:132,141`, `api/history.py`, `frontend/src/history/*` | `FreeLibrary.tsx` (covers), `Shelf.tsx:480` (history, local only) | Not solo features: reading-history import is planned for hosted (ADR 0041 part 11, second half; handoff item 2) |
| K11 | The root `.env.example` | `.env.example` | Documents the workshop's settings | Stays as the workshop's example; lines for removed settings go with their pull requests |

### Remove

| # | What it is | Files | What depends on it (found) | Why it goes |
|---|---|---|---|---|
| R1 | `spikes/`: the first experiments, before the real code existed | `spikes/` (40 files, about 600 KB) | Nothing imports it. Seven module docstrings mention where code was copied from (e.g. `app/ingest/extraction/pipeline.py:2`); `tests/eval/fixtures/chapters_meta.json` names `spikes/…/chXX.txt` as text, but `tests/eval/_fixture.py:38` reads its own copies in `tests/eval/fixtures/` | History only; git keeps it |
| R2 | One-off measurement scripts | `scripts/benchmark_cold_reader.py`, `benchmark_conversation.py`, `benchmark_direct_api.py`, `benchmark_jev_conversation.py`, `benchmark_scene_review.py`, `benchmark_selection_review.py`, `jev_identity_audit.py`, `jev_integration_audit.py`, `jev_quality_audit.py`, `jev_reuse_audit.py`, `prepared_reuse_audit.py`, `reader_preparation_audit.py`, `classic_books_audit.py`, `real_book_audit.py`, `live_book_audit.py`, `live_quality_audit.py` (about 4,200 lines) | Only each other (`classic_books_audit` imports `real_book_audit`; `live_book_audit` imports `live_quality_audit`) and 4 test files that test the scripts themselves: `tests/scripts/test_audit_reporting.py`, `test_book_source_capture.py`, `test_live_book_audit.py`, `tests/api/test_real_book_audit.py` (about 760 lines). Mentioned in `docs/product/companion-audit-2026-11-14.md` and `docs/jev-integration.md`; nothing in CI runs them | Each answered one question once; most measure paths removed below (prepared answers, reuse) or the owner's own library. Git keeps them |
| R3 | The answer cache and answer reuse: a reviewed answer kept in a file and served again when the same (or a similar) question comes back | `answer_reuse.py`; the cache part of `answer_bank.py`; `READER_ANSWER_CACHE`, `READER_REUSE_ENABLED`, `_FAST_REVIEW`, `_MIN_PROBABILITY`, `_MIN_CONFIDENCE`, `_TIMEOUT_SECONDS` (`config.py:55,81-85`); `reviewed-answers-v2.json` in the data folder (`deps.py:34`) | Off by default; hosted refuses it (`config.py:253`). Only acts when on: `views.py:611,676-714`, `page_conversation.py:150,206,459`. `AnswerBusy` and `valid_answer` in `answer_bank.py` are also used by the live paths and stay. Tests: `test_prepared_response.py` (whole), parts of `test_reader_latency.py`, `test_takeover_regressions.py`, `test_page_read_frontier.py`, `test_foundations.py`, `llm/test_gpt6_models.py` | A speed-up for one person asking the same things; it can never be in hosted (one file for every reader) |
| R4 | Prepared questions: suggested questions whose answers were written in the background ahead of time | `reader_prepare.py`, `preparation_plan.py`, `GET /api/books/{id}/prepared-questions` (`views.py:248`), `READER_PREPARE_ENABLED`, `_MAX_QUESTIONS`, `_DELAY_SECONDS`, `_MAX_USD_PER_BOOK` (`config.py:86-89`); frontend `api.preparedQuestions` and the suggestion effect in `AskTheBook.tsx:793-806` | Off by default; hosted refuses it (`config.py:255`) and has no such route (the shelf asks once and gets 404, see the open items). The route returns an empty list unless the cache (R3) is on (`views.py:257`). Tests: `test_reader_preparation.py`, `test_preparation_plan.py`, `test_preparation_priority.py` (whole), parts of `test_foundations.py`, `test_takeover_regressions.py`, `test_primary_reading_context.py` (imports `preparation_facts`) | Paid background work for one reader; replaced by the streamed chat and the home chat's suggestions |
| R5 | Recap pre-warming and the recap file cache: catch-up summaries written in the background and kept in a file | `recap_warm.py`; `RECAP_PREWARM`, `RECAP_DISK_CACHE` (`config.py:53-54`); `reviewed-recaps-v1.json` (`main.py:555`); `warmer` in `main.py:558-566`, `api/reading.py:247,280`, `api/views.py:298` | Off by default; hosted refuses the file cache (`config.py:254`) and has no catch-up route. Tests: `test_recap_prewarm.py` (whole), parts of `test_preparation_priority.py`, `test_reader_preparation.py`. The in-memory cache (`RecapRegistry`, K9) stays | Same as R4 |
| R6 | "Fast mode": paying OpenAI's priority price for quicker local replies | `READER_FAST_MODE` (`config.py:80`), `deps.py:25-31` | 1 test file (`test_reader_latency.py::test_reader_fast_mode_is_isolated_and_bills_the_fast_premium`) | A setting for one person's local replies on a paid key; the hosted writer is built elsewhere |
| R7 | The retired "ingest a book on the reader's key" job and its job routes | job kind `ingest_book` (schema `0008`, `0021`, `0025`); `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel` (`tenant/api.py:668-708`); `repository.py:2071-2160,2531-2587`; credential lookup for a job (`jobs.py:187-241`, `worker.py:16-18,74-82,139`) | No handler was ever written (`tenant/api.py:631-635`); only made while `HOSTED_READER_KEYS` is on and story memory off. No frontend caller of `/api/jobs`. The reader's "jobs under way" count, checked against a limit, counts only this kind (`repository.py:228,1870`), so it is always zero once the kind is gone and goes with it. **But the generic job tests use `ingest_book` as their sample job**: `test_jobs_postgres.py:141,479` (leases, claims, fencing, cancellation), `test_worker.py`, and mentions in `test_migrations.py`, `test_companion_notes_postgres.py`, `test_story_memory_jobs_postgres.py`, `test_account_deletion_postgres.py`, `test_tenant_postgres.py` | Story memory jobs replaced it (ADR 0042 section 6). Needs a database migration and the job tests moved onto a living kind first |
| R8 | The README's description of Mothli as a single-user, self-hosted app | `README.md:1-13` | Nothing | It describes the old product; rewrite to "a hosted reading app; local mode is for development" |

### Owner must decide (the questions at the end)

| # | What it is | Files | What depends on it (found) | Recommendation |
|---|---|---|---|---|
| D1 | A reader pasting their own AI account key into hosted Mothli, and the screen for it | `hosted/credentials.py` (403 lines), `hosted/provider_settings.py` (218), routes `/api/credentials*` and `/api/provider-settings*` (`tenant/api.py:326-520`), `repository.py:2270-2481`; tables `provider_credentials` and `provider_model_settings` (schema `0003`, `0009`, `0010`, row security in `0004`), `jobs.credential_id`; settings `HOSTED_READER_KEYS`, `HOSTED_CREDENTIAL_MASTER_KEY`, `HOSTED_CREDENTIAL_KEY_VERSION`, `HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS`, `HOSTED_PROVIDER_VALIDATION_TIMEOUT_SECONDS`; `frontend/src/shelf/ProviderSettings.tsx` (561 lines) and its test (293), the settings button in `Shelf.tsx:20,137,209-212,910-912`, `api.ts:975-999` | Off today (`config.py:171`); the routes are not even mounted (`tenant/api.py:1720-1730`). The code says it is "kept for a later 'bring your own key' option" (`tenant/api.py:1703-1706`). **`HOSTED_CREDENTIAL_MASTER_KEY` and `_KEY_VERSION` are required to start hosted mode at all** (`config.py:242-243`, `deploy/app.env.example:16-18`), and the worker builds the key decrypter at start (`worker.py:139`), although nothing the owner pays for uses them. Tests: `test_credentials.py`, `test_provider_settings.py` (whole); `test_platform_pays.py`, `test_tenant_postgres.py` (62 mentions), `test_jobs_postgres.py`, `test_migrations.py`, `test_worker.py`, `test_deploy_roles_postgres.py`, `test_health_ready.py`, `test_runtime.py`, `api.test.ts`, `Shelf.test.tsx` (parts). Also `tenant/grants.py:37-38,68-69`, `scripts/hosted_roles.py`, `tenant/export.py:65-66` | Remove (question 1) |
| D2 | The older replies in hosted mode that run on the reader's own key: ask, "Explain / Define" on a picked passage, and the end-of-chapter look back | `POST /api/books/{id}/ask`, `/selection-action`, `/chapter-closeout` (`tenant/api.py:875-1300`); `hosted/provider_runtime.py` (128); `repository.py` `ask_context`, `selection_action_context`, `chapter_closeout_context`, `reserve_provider_call` (lines 412-660) | Not mounted while D1 is off. Self-contained: nothing else calls these repository methods (searched). Tests: `test_provider_runtime.py` (whole), `test_tenant_postgres.py::test_ask_the_book_is_owner_scoped_spoiler_bounded_and_costed` and `::test_reading_assistance_is_owner_scoped_frontier_bounded_and_costed`, `llm/test_gpt6_models.py` imports `budgeted_hosted_completion` | Remove with D1 (question 1). "Explain / Define" for hosted readers is still wanted (handoff item 2); it should be built on the platform's models and the read map, as the streamed chat is, not on this code, which goes by the chapter counter |
| D3 | Moving a library from the local app into a hosted account | `hosted/local_migration.py` (1,016 lines), table `local_library_migrations` (schema `0013`), `docs/runbooks/local-to-hosted-migration.md` | Nothing in the app imports it; 1 test file (`test_local_migration.py`, 6 tests, none about spoilers). Mentioned in `tenant/export.py:77`. **Already broken**: it writes no `book_search_documents`, so a moved book would open with no chapters (open items, "walk, 17 November"); it also carries no notes, chats, read map or memories (go-live map) | Remove (question 2) |
| D4 | Running local Mothli as an always-on service on the owner's computer, with daily backups | `scripts/run-local-production.sh`, `scripts/stop-local-production.sh` | Nothing but a mention in `docs/product/go-live-map-2026-11-17.md:189`. Calls `python -m app.lifecycle backup-all` (K9) | Remove (question 2) |

Count: 11 keep as harness, 8 remove, 4 for the owner to decide (two questions).

### Looked at and left alone (not solo-only)

- Experimental checker switches (`JEV_RECAP_ENABLED`, `JEV_PARTIAL_EVIDENCE_ENABLED`, `JEV_EXTRACTION_ENABLED`, `JEV_IDENTITY_ENABLED`): the last two act inside chapter extraction (`ingest/chapter_extraction.py`), which hosted story memory runs. A separate clean-up of experiments, not of solo use.
- `HOSTED_READER_STORY_MEMORY`: the owner's own pending decision (ADR 0042), not solo.
- `GET /api/limits` and `GET /api/costs` in hosted: no screen calls them, but they read the real caps and spend; not solo.
- `HOSTED_PROVIDER_ALLOWED_ORIGINS`: still checks the platform writer's address (`config.py:276-284`).
- `frontend/src/reader/ProviderCost.tsx`: shows what an "Explain" cost, used in local mode by `SelectionAssistant.tsx:170`.
- `security_best_practices_report.md` and the other dated documents: records of their day, not solo use.

## What each removal costs in tests and the database

The project's rule: **no spoiler test is weakened or deleted without an equal one in its place.**
For each removal, the spoiler tests that go and the equal ones. None of the removals below takes a
layer of spoiler protection away from anything a reader still uses: each removes a whole path, and
the paths that stay keep every check they have today.

**R1 spikes.** No tests. No migration.

**R2 measurement scripts.** Four test files go. They test the scripts, not the product, but three
have spoiler-sounding names, so each was read:
`test_real_book_audit.py::test_unread_canary_is_recorded_and_blocked_before_provider_call` tests the
script's own tripwire (`install_observers` from `scripts/real_book_audit.py`), which exists only
while the script runs; `test_audit_reporting.py::test_book_harness_checks_effective_frontier…` and
`test_book_source_capture.py::test_checks_validate_all_candidates…against_same_frontier` check that
the scripts measure at the right place. The product's own checks that they watched stay tested by
`test_page_read_frontier.py::test_forged_larger_boundary_is_withheld_without_a_model_call` and
`test_views.py::test_ask_the_book_withholds_an_uncited_future_answer_without_leaking_it`. No migration.

**R3–R6 the solo speed-ups.** These spoiler tests go, because what they guard goes:
- `test_reader_latency.py::test_reviewed_answer_cache_is_free_and_frontier_and_reset_scoped` and
  `test_reader_preparation.py::test_semantic_reuse_is_unchanged_and_never_considers_future_or_retracted_scope`,
  `::test_bank_restart_bounds_origin_and_corruption`, `test_prepared_response.py::test_packet_keeps_each_claims_exact_proof_separate_and_omits_future`,
  `::test_future_or_title_only_name_cannot_supply_question_identity` (a stored answer from further on is never served again).
  Equal: **write first** a test that, with every old cache setting given, nothing is stored between
  two questions and the second is answered fresh (and that the old setting names are refused or
  ignored, so the cache cannot come back by a forgotten `.env` line). The live-answer checks stay in
  `test_views.py::test_ask_the_book_withholds_an_uncited_future_answer_without_leaking_it` and
  `test_page_read_frontier.py`.
- `test_recap_prewarm.py::test_prewarm_is_cached_and_never_reuses_a_future_or_deleted_book`. Equal
  that stays: the in-memory recap cache tests `test_views.py::test_catch_me_up_cache_survives_a_future_chapter_ingest`,
  `::test_catch_me_up_judge_blocks_a_future_reference`, `::test_catch_me_up_at_zero_reads_nothing`.
- `test_preparation_plan.py::test_context_adds_returning_cast_states_and_ties_without_unread_or_expired_fields`,
  `test_preparation_priority.py::test_unready_frontier_waits_for_ingest…` and `::test_checkpoint_bounds…`:
  they guard what background preparation sends to a model. Nothing sends it once R4 is gone; the
  first new test above covers that nothing is prepared.
- `test_primary_reading_context.py` imports `preparation_facts`; check whether the live context uses
  it before deleting `preparation_plan.py`. Searching found no caller in `app/` outside the removed
  files, but this test may hold the last use; if so, keep the function or move it with the test.
No migration (files in the local data folder only; the pull request should say that old
`reviewed-answers-v2.json` and `reviewed-recaps-v1.json` files can be deleted by hand).

**R7 the retired book job.** No spoiler test. The job machinery tests (leases, owner and book
fencing, cancellation, failure codes) must first be moved onto a living job kind (`chapter_notes`
or `story_memory`), test by test, so the machinery stays proven; then the kind can go.
`test_jobs_postgres.py::test_worker_claim_is_owner_and_book_fenced_against_foreign_identifiers` is a
security test (one reader's job cannot touch another's book) and must keep an equal on the new kind.
**Migration needed**: a new one that takes `ingest_book` out of the job-kind checks (from `0025`).
Nothing is deployed, so no rows exist; the migration still has to be a new file, since migrations
are forward-only and an applied file's checksum may never change
(`test_migrations.py::test_migrations_are_contiguous_forward_only_files`,
`::test_applied_migration_checksum_and_history_are_immutable`, `hosted/migrations.py:92`). The same
applies to every migration below. Two tests in `test_migrations.py` (`:283`, `:300`) are about the
key tables and go with D1.

**R8 README.** No tests.

**D1 + D2 the reader's own key, if the owner agrees.** Two spoiler tests go with the routes:
- `test_tenant_postgres.py::test_ask_the_book_is_owner_scoped_spoiler_bounded_and_costed` (hosted
  `/ask` stays within the reader's place and the reader's own books). Equal that stays, on the
  hosted chat readers really use: `test_companion_stream_postgres.py::test_second_reader_cannot_stream_from_first_readers_book`,
  `::test_a_sentence_naming_someone_not_yet_met_is_removed`, `::test_a_skimmed_page_never_reaches_a_model`,
  and `tests/parity/test_companion_stream_parity.py::test_the_same_reading_and_reply_give_the_same_events_locally_and_hosted`.
- `test_tenant_postgres.py::test_reading_assistance_is_owner_scoped_frontier_bounded_and_costed`
  (hosted "Explain / Define" and the chapter look-back). **There is no hosted equal**, because no
  hosted reader can reach these today (the button is hidden, `ReaderPage.tsx:192-195`). After
  removal nothing in hosted serves them, so nothing is left unguarded. The local equals stay:
  `test_views.py::test_selection_actions_use_only_the_visible_selection_and_return_its_anchor`,
  `::test_chapter_closeout_is_exact_chapter_cited_and_completed_only`,
  `test_read_map.py::test_help_with_a_selected_passage_works_wherever_the_reader_is`. When
  "Explain / Define" is built for hosted readers, its own hosted spoiler test must be written before
  it is switched on.
- `test_platform_pays.py::test_reader_key_routes_answer_as_unknown_paths_unless_reader_keys_are_on`
  becomes "these paths do not exist in hosted mode, whatever the settings", and stays.
**Migration needed**: a new one dropping `provider_model_settings`, `provider_credentials` and the
`jobs.credential_id` column with its link and index. The tests that find tables from the
database's own catalogue (account erasure `test_account_deletion_postgres.py:261`, book removal
`test_removing_a_book_postgres.py:100`, export `test_account_export_postgres.py:100-104`) will then
fail until `tenant/export.py`'s `LEFT_OUT` list, `tenant/grants.py` and `scripts/hosted_roles.py`
stop naming the dropped tables; that failure is the check working, and tells the builder what was
missed. `deploy/app.env.example` loses three lines; `config.py` stops requiring the master key.

**D3 + D4 the move from the local app, if the owner agrees.** No spoiler tests (the 6 tests in
`test_local_migration.py` are about copying safely and undoing). **Migration needed** for D3: drop
`local_library_migrations`, then take it out of `tenant/export.py:77` (the export test fails until
then, as above). D4 needs none.

## Order of pull requests

Smallest risk first. Each one is green on its own (backend suite, hosted and parity suites against
a test database, frontend tests and build, `ruff check app tests scripts`).

1. **Remove `spikes/`** (R1). Deletion only, 40 files. Extra-small.
2. **Remove the measurement scripts** (R2) and their 4 test files. About 5,000 lines deleted, no
   product code touched. Small.
3. **README and the go-live map say what Mothli now is** (R8). Words only. Extra-small.
4. *If question 2 is yes:* **remove the service scripts and the local-to-hosted move** (D4, D3):
   two shell scripts, `local_migration.py`, its test and runbook, one migration, one line of
   `export.py`. Small.
5. **Remove the solo speed-ups** (R3–R6). First write the "nothing is kept between questions" test;
   then remove the cache, reuse, prepared questions, pre-warming, the recap file cache and fast mode,
   their settings and `.env.example` lines, and the suggestion chips in `AskTheBook.tsx`. About 1,200
   lines of app code and 7 to 9 test files deleted or trimmed. Medium.
6. *If question 1 is yes:* **remove the reader-key routes and the settings screen** (D2 and the
   front half of D1): the ten routes, `provider_runtime.py`, `ProviderSettings.tsx`, the shelf's
   settings button, the repository methods. No database change yet. Medium.
7. **Retire the book job** (R7): move the job machinery tests onto a living kind first, then remove
   `ingest_book`, the `/api/jobs` routes and the worker's key lookup, with one migration. Medium; the
   riskiest, because it touches the background worker every hosted feature uses.
8. *If question 1 is yes:* **drop the key tables and settings** (rest of D1): `credentials.py`,
   `provider_settings.py`, one migration, `HOSTED_READER_KEYS` and the three key settings, grants,
   roles, `export.py`, `app.env.example`. Medium.

- Built on 18 November 2026 in PR #112: steps 1 to 3 as designed, one commit each, except (1) the
  spike is named in 18 files under `backend/app`, not seven (16 module docstrings and the header
  comments of `memory.sql` and `catalog.sql`); each now says "the removed spikes/…", nothing else in
  them changed; the four test docstrings and the older ADRs that name the spikes were left as
  history; `chapters_meta.json` was not edited, and the eval tests pass on their own copies;
  (2) the 4 deleted test files held 53 tests: the non-hosted suite collects 2,280 on `main` and 2,227
  here; besides the script tests, `test_real_book_audit.py` held four cost tests of
  `reader_prepare.PreparationCatalog` (foreground headroom under the book caps), which go now
  rather than with R4 in step 5; (3) `backend/README.md`, which the record did not list, told readers
  to run `live_quality_audit.py` and `live_book_audit.py`; those passages now say the scripts were
  removed; (4) the go-live map's only statements made false were two README references (a line
  number and the quoted status sentence); the open item on `benchmark_conversation.py`'s missing
  spend cap is closed by its removal.
- Built on 18 November 2026 in PR #115: step 5 as designed, the equal test first
  (`test_nothing_kept_between_questions.py`), then one commit per piece in the order R6, R5, R4, R3
  (smallest first, and the only order that keeps every commit green: `recap_warm` imports
  `reader_prepare`, which imports `answer_bank`, which imported `preparation_plan`), except
  (1) `Settings` ignores unknown names (`extra="ignore"`), so the old names are ignored, not refused;
  the test checks that none becomes a setting; (2) besides what the record listed, these went
  because only the removed pieces used them: the ingest worker's `on_complete` hook and the
  `recap_preparation` flag in `book_lifecycle` (R5), the file load and save in `RecapRegistry` (R5),
  the background client's `preparing` flag, `PREPARED_ANSWER_SYSTEM` and the `PreparedQuestions`
  effort entry (R4), `jev_tasks.reuse_answer` and the answer-scope hash in `page_conversation.py`
  (R3); (3) left in place: `AnswerBusy` and `valid_answer` stay as the record says, though nothing
  in `app/` calls them any more, and the LLM client's fast-tier support (`service_tier`, the `fast:`
  prices, `reuse_review_profile`), shared code that nothing now switches on; (4) tests: 144 test
  items deleted and 10 added (2,227 collected on `main`, 2,093 here); two spoiler tests the record
  did not name went with what they guarded (`test_primary_reading_context.py::test_reuse_subset_…`
  and `test_lifecycle_cache.py::test_cleared_recaps_survive_restart_…`), with the same equals;
  `test_reader_latency.py::test_reviewed_answer_cache_…`, listed to go, also guarded the live
  frontier and reset checks and was trimmed instead; `test_primary_reading_context.py` keeps its
  snapshot test, now checking `primary_snapshot` at chapter 2 where it checked `preparation_facts`,
  and loses the plan-only test; the `valid_answer` half of `test_bank_rejects_invalid_answers` is
  kept as `tests/test_answer_bank.py`; (5) docs: `jev-integration.md` says the speed-ups are gone
  and keeps the old paragraphs as record; the README and the go-live map lose their mentions.
- Built on 18 November 2026 in PR #116: step 4 as designed (the two shell scripts,
  `local_migration.py`, its test and runbook, one line of `export.py`), except (1) the table is
  dropped by migration `0027_drop_local_library_migrations.sql` (its policy, then the table, which
  takes its checks, unique indexes and owner link); no role was ever granted on it by name, so
  `tenant/grants.py` and `scripts/hosted_roles.py` needed no change, and no schema constant or
  deploy file named it; (2) besides `export.py`, the book-removal test named it, in its kept list
  and its "may be empty" set, and in a docstring; all three went; (3) the 6 deleted tests were
  about copying safely and undoing, none about what a reader may see; the hosted and parity suites
  collect 432 at d32617e and 426 there (on `main` after step 5, 426 and 420); (4) nothing imported
  `local_migration.py`; `lifecycle/archive.read_portable_archive` loses its only caller in `app/`
  and stays with K9; (5) `hosted/companion/store.py:66-69` still says a moved-in book carries its
  progress in the older counter: left alone, as reading-position code; the branch it describes
  (`store.py:89`) is not only for moved-in books: a server run with the read map off raises that
  counter from device reports (`tenant/api.py:1584`, `repository.py:1215`), though with the map on
  only a moved-in book or such an earlier period could have left it above 0; (6) ADR 0030's link to the removed runbook is left as history.
- Built on 18 November 2026 in PR #117: step 6 as designed (D2 and the front half of D1, no
  database change), the equal test first, then D2, D1's routes, the frontend, the tidy-ups left by
  step 5 and these docs, one commit each, except (1) the streamed chat had no test that a later
  place claimed by the browser (a bookmark of 99, a read point naming later chapters) cannot widen
  what a reply reads, which the hosted `/ask` test checked for its path; it was written first
  (`test_companion_stream_postgres.py::test_a_place_the_browser_claims_cannot_widen_what_a_reply_reads`);
  the rest of that test's properties have the named equals (another reader's book:
  `::test_second_reader_cannot_stream_from_first_readers_book`; the read map as the bound:
  `::test_a_skimmed_page_never_reaches_a_model`, `::test_a_sentence_naming_someone_not_yet_met_is_removed`;
  cost recorded to the asking reader: `::test_a_stored_turn_and_its_cost_belong_to_the_asking_reader`;
  hosted equals local: the parity test); (2) besides what the record listed, these went because
  only the removed routes used them: `_CredentialBodyLimit` in `main.py` and the cipher, validator
  and origins the web app built at start-up for the routes alone (the worker still builds its own
  cipher, and `config.py` still requires the master key until step 8), the tenant repository's
  credential and provider-setting methods (`create_credential` to `resolve_credential`) and
  `settle_provider_call`, the `CREDENTIAL` and `PROVIDER_SETTINGS` lock kinds, and
  `hosted/provider_settings.py` with `test_provider_settings.py`, since nothing else imported it;
  the endpoint inventory loses its ten entries and the `credential` surface class (version 24);
  (3) `HOSTED_READER_KEYS` was left gating only an upload's retired `ingest_book` job, which waits
  for a key no reader can now give and holds the reader's job limit for good; hosted settings
  refuse it being on, naming it (`test_platform_pays.py::test_hosted_settings_refuse_reader_keys_…`),
  and an upload makes no extraction job; the tests that made that job by uploading with the
  setting on (the source lifecycle and audit tests in `test_tenant_postgres.py`, account deletion's
  seed) now write it by hand so the job routes keep their checks until step 7;
  (4) frontend: besides the screen, its test, the button and `api.ts`, hosted mode no longer asks
  the server once a session whether it helps with a picked passage (`api.passageHelp`, whose probe
  was a request that could only fail), and "Tell me" after a chapter asks the chat directly in
  hosted mode instead of first trying `/chapter-closeout`; the screen's styles went; local mode
  keeps Explain / Define and the look-back; (5) tests: backend (not hosted) 2,093 collected on
  `main`, 2,080 here (the hosted half of the GPT-6 cache billing test, 4 items, and the tidy-ups'
  9); hosted and parity 420 on `main`, 413 here (+1 equal, +3 for the "do not exist, whatever the
  settings" test and +1 for the refusal in place of the old routes test, -2 `test_provider_runtime.py`,
  -4 `test_provider_settings.py`, -4 in `test_tenant_postgres.py`, -1 body-limit test); frontend
  493 to 481 (-9 `ProviderSettings.test.tsx`, -3 in `api.test.ts`, -1 in `Shelf.test.tsx`, +1 for
  "Tell me"); (6) tidy-ups: `answer_bank.py` and its test went (no caller); the client's Fast tier
  went (`service_tier` is now always `default`, `fast:` billing and its settle-at-Standard branch,
  `reuse_review_profile`), every model still billed at Standard prices; the optional `model`
  argument of `note_reservation_actual` stays, unused; `store.py`'s comment no longer names the
  move. Left for step 7: the `ingest_book` kind, `/api/jobs`, the worker's key lookup
  (`jobs.py`, `worker.py`), `create_uploaded_book`'s `extraction_job` parameter and its read of
  `provider_model_settings`, and the limit check that counts that kind. Left for step 8:
  `hosted/credentials.py` and its test, the two tables and `jobs.credential_id`, the setting
  `HOSTED_READER_KEYS` itself and the key settings (`HOSTED_CREDENTIAL_MASTER_KEY`, `_KEY_VERSION`,
  `_PREVIOUS_MASTER_KEYS`, `HOSTED_PROVIDER_VALIDATION_TIMEOUT_SECONDS`), grants, roles,
  `export.py` and `deploy/app.env.example`.

If question 1 is no, steps 6 and 8 are skipped and step 7 keeps the worker's key lookup.

## Not in this round

- The non-streamed local reply (K5). Once every spoiler test that goes through `/ask` has an equal on
  the streamed path, and the workshop can run Jev cheaply enough, it can follow. That is roughly 30
  tests in 12 files to move first; not worth doing before going live.
- The experimental checker switches listed above.

## Questions for the owner

Both answered on 18 November 2026: **no** to question 1 ("users cannot pay the Mothli AI with their
own AI account, they can only pay us") and **no** to question 2 ("I don't need anything from the
copy of Mothli on my own computer"). Every conditional step in the order above goes ahead.

**1. Should readers ever be able to pay for Mothli's AI with their own AI account?**
Today you pay for every reader's AI, with daily and monthly caps. Old code is still there, switched
off, that would let a reader paste in their own key from an AI company and pay that company
directly. Keeping it costs effort: the server must be given a secret key at start-up only for this
switched-off feature, and its screens and tables must be kept safe and tested. Removing it means
that if you ever want "bring your own key", it is rebuilt from the project's history.
*Recommendation: no; remove it.* A reader who "knows nothing how AI works" will not have such a key,
and you have already chosen to pay with caps.

**2. Do you still need anything from the copy of Mothli on your own computer?**
Two things exist only for that copy: a tool that moves your books and reading place from it into
an online account, and scripts that keep it running all day with daily backups. The moving tool is
already broken (a moved book would open with no chapters) and does not carry your chats, notes or
what Mothli remembers. If you want your own books online, adding them again in the hosted app is
simpler and safer.
*Recommendation: remove both.* The copy on your computer stays for building and testing Mothli; it
just stops being a place where you keep your own reading.

## What this record could not verify

- Nothing was run. Line counts and test counts come from searching the files; whether each pull
  request is green is for its builder to show.
- Whether the owner's own local settings file turns any of R3–R6 on. It was not opened (a rule of
  the project). If it does, his local Mothli will behave differently after step 5: no stored
  answers, no suggested questions.
- Whether `preparation_facts` (R4) has a live use that searching missed (see above).
- The outside tools in `C:/Users/Admin/mothli-dev-tools/` were searched in review: none imports a
  script or module named here, but `measure_extraction.py:59` and `run_later_name_probe.py:56` pass
  the settings `reader_answer_cache` and `reader_prepare_enabled`, and each needs those two words
  taken out after step 5.
- Frontend: `api.test.ts` checks that a route hosted mode lacks is asked for only once
  (`api.test.ts:555-578`) using `/api/provider-settings` as an example; it needs another example
  route after step 6. Not checked further.
