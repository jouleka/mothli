# Going live: a map of hosted mode and the gap to the companion

17 November 2026. Read from `main` at 6971f14 (PR #82). Nothing was changed, migrated or started; no model was called. The hosted tests were run once on this machine (section B8).

## A. For the owner

**Where hosted mode stands.** The repository has a real, careful foundation for many readers: sign-in through an outside sign-in service, one account per person, every row in a Postgres database marked with its owner and walled off from other owners, book files stored encrypted, per-reader limits, and tests that try to read another reader's data. On top of that foundation a reader can upload a book, read it, keep their place across devices, keep highlights and bookmarks, and ask the older, non-streamed question path, paid for with an AI key the reader pastes in themselves. None of the recent companion work is there: no read map, no stored conversation, no chapter notes, no home chat, no "what Mothli remembers", no suggestions, no free library, no history import. The settings refuse to switch the companion on in hosted mode at all, and nothing has ever been deployed: there is no container, no hosting setup and no way to build a book's cast in the background.

**If it were switched on today, a reader could:** sign in, upload an EPUB, read it, have their place saved and synced, highlight, annotate, bookmark, change the page look, search the book, and ask questions or get a chapter wrap-up once they paste their own OpenAI or Anthropic key.
**They could not:** talk to Mothli as it is now (streamed, checked sentence by sentence, following what they actually read), see Mothli's notes on chapters, use the home chat, have anything remembered about them, get suggestions, add a free classic, import their reading history, see the cast or a recap, or delete their account. A book's cast and story memory are never built, because the background worker has no job to run.

**Decisions only you can make** (details in B10):
1. Where it runs (which host, which managed Postgres, which file store).
2. How people sign in (Google, Apple, email and password through a sign-in company, or several).
3. Who pays for the AI: each reader brings their own key (what the code assumes today), you pay with a cap per reader, or a mix.
4. The spending cap per reader and per day, and what a reader sees when it is reached.
5. Limits on free use (books, storage, questions).
6. What happens to a reader's data when they delete a book or their account, and for how long backups keep it.
7. Terms and a privacy policy (none exist in the repository).
8. Whether two readers with the same book may share Mothli's chapter notes (cheaper) or never share anything (simpler to promise).

**Proposed order of work** (B11 has files, tests and what can run side by side):
1. Split the hosted code into one file per feature, and agree migration numbers. Small.
2. Proof: the read map in hosted mode, two readers who cannot see each other's rows. Medium.
3. Stored conversations inside a book. Medium.
4. Book text and story memory for hosted books (the stand-in for the local per-book store). Medium.
5. Paying for the companion: models, the checker, ledgers and caps, per reader. Medium to large; needs decisions 3 and 4.
6. The streamed chat inside a book. Large.
7. Background work for many readers (chapter notes, what Mothli remembers, story memory). Large.
8. Home chat, what Mothli remembers, suggestions. Large.
9. Deployment: containers, serving the app, database roles, backups, health checks. Medium to large. Can start now.
10. Free library and history import in hosted mode. Medium each. Can start after step 1.
11. Accounts: delete and export, sign-in screens, the page where a reader sets up AI. Medium. After decisions 2, 3, 6, 7.

## B. For builders

File and line references are to 6971f14. "Local" is `DEPLOYMENT_MODE=local` (the default), "hosted" is `DEPLOYMENT_MODE=hosted`.

### B1. How hosted mode is switched on and structured

- **Switch.** `deployment_mode: Literal["local","hosted"] = "local"` (`backend/app/config.py:90`), read from the environment or the repo-root `.env` (`config.py:17,21`). Hosted startup refuses to run without an explicit `TRUSTED_HOSTS` (`config.py:189-191`) and without `HOSTED_AUTH_DSN`, `HOSTED_TENANT_DSN`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI`, `HOSTED_CREDENTIAL_MASTER_KEY`, `HOSTED_CREDENTIAL_KEY_VERSION` (`config.py:193-205`). Since 18 November the last two no longer exist and are not needed to start (ADR 0043 step 8).
- **The companion is refused in hosted mode by the settings.** `reader_conversation_jev` requires local mode (`config.py:170-172`); streamed replies require it (`config.py:164-165`); the read map requires streamed replies (`config.py:178-179`); recollections require the read map (`config.py:182-183`); chapter notes follow streamed replies (`config.py:176-177`); deferred memory ingest requires local (`config.py:160-161`); `codex-local` requires local (`config.py:184-185`).
- **Two separate apps in one factory.** `create_app` (`backend/app/main.py:327`) builds a hosted lifespan (`main.py:347-408`) with a Postgres auth repository, a Postgres tenant repository, object storage, an OIDC client, a per-process lock/cache registry, a credential cipher and a provider validator. It never builds `Catalog`, `Store`, the LLM client, `IngestWorker`, `NotesBuilder` or `Recollector`; those exist only in the local lifespan (`main.py:410-462`, notes and recollections at `main.py:442-448`).
- **Routers.** Hosted includes only `auth_api.router` and `tenant_api.router` plus `/api/health/live` (`main.py:482-493`). None of the local routers `backend/app/api/*.py` is mounted in hosted mode (`main.py:494-505`). So hosted mode has its **own parallel API** in `backend/app/hosted/tenant/api.py` (1657 lines), not the local routers over another store.
- **How a request finds its reader.** Every tenant route depends on `_owner` or `_csrf_owner` (`tenant/api.py:250-275`): the session cookie gives a principal, `OwnerId(session.principal.owner_id)` is taken from it, a per-owner request limit is consumed, and any query parameter named like an owner or storage id is refused (`tenant/api.py:233-247`). Client-supplied owner ids are never accepted (`tenant/api.py:1`).
- **How the store is abstracted.** It is not: `PostgresTenantRepository` (`backend/app/hosted/tenant/repository.py:42`, 2652 lines) is async psycopg with its own SQL, method names unrelated to `Catalog`. Every transaction sets `app.owner_id` for row-level security (`repository.py:49-54`) and every query also carries `owner_id` explicitly. At startup it checks that its database role holds exactly an allow-list of table privileges and nothing else (`repository.py:56-143`); a new table must be added to that list, to the role's grants, and to the test fixture grants (`backend/tests/hosted/test_tenant_postgres.py:80-84`). A separate sync repository exists for the worker (`backend/app/hosted/jobs.py:93`), which is a precedent for a sync Postgres adapter.
- **Endpoint inventory.** `backend/app/hosted/tenant/endpoints.json` lists every hosted route with its cross-tenant test; `backend/tests/hosted/test_tenant.py:28,354` fail if a route is added without an entry.
- **The companion modules take `catalog`, `store` and `worker` as plain arguments** (`read_map.py`, `stream_conversation.py:371,487`, `chapter_notes.py`, `recollections.py`, `home_conversation.py`), all synchronous, calling `Catalog` methods and `store.book(book_id)` (the per-book `memory.db`: `stream_conversation.py:385,586`, `recollections.py`). This is the seam a hosted port can use: an owner-bound object with the same method names.

### B2. Accounts and sign-in

**Exists.**
- Sign-in only through one outside OpenID Connect provider: `GET /api/auth/login`, `GET /api/auth/callback`, `GET /api/auth/session`, `POST /api/auth/logout` (`backend/app/hosted/auth/api.py:81,121,217,236`; flow in `auth/oidc.py`). PKCE, signed ID tokens, one-time state (`backend/README.md`, section "Hosted OIDC and sessions"; ADR 0020).
- Sign-up is implicit: the first sign-in creates a `users` row (`backend/app/hosted/auth/repository.py:200`), and a database rule gives every new user default limits (`schema/0011_owner_limits.sql:22-23`). One outside identity maps to one user; email is profile data, not a key (`schema/0001_identity_and_accounts.sql:20-34`, ADR 0020 lines 34-37).
- Server-side sessions, stored only as digests, cookies `__Host-litlet-session` (HttpOnly) and `__Host-litlet-csrf`, Secure, SameSite=Lax (`auth/api.py:21-22,52-71`). Lifetime 8 hours absolute, 30 minutes idle by default (`config.py:131-132`).
- CSRF: double-submit token checked on every changing request (`auth/api.py:200-214`, used by `_csrf_owner`).
- Per-owner request rate limit, 600 a minute by default (`schema/0011_owner_limits.sql:8-10`, `repository.py:145`).
- Per-owner AI keys ("credentials"), envelope-encrypted with a master key from the environment (`backend/app/hosted/credentials.py`; routes `tenant/api.py:344-417`; ADR 0017 section 8, ADR 0025). Removed on 18 November with its tables and master key (ADR 0043, D1).
- Content-free audit log with its own role and a purge command (`backend/app/hosted/audit.py`; `backend/README.md`).

**Missing.** Passwords and email sign-in (by design: delegated to the provider), password reset, email sending of any kind, more than one sign-in provider at once (one `OIDC_ISSUER`, `config.py:122`), linking two identities, roles or an admin surface (operator tools are command-line scripts: `hosted/limits.py`, `hosted/audit.py`), account deletion or export (no route; `users.status` allows `deleting` and `deleted`, `schema/0001...sql:10`, but nothing sets them), rate limits on the sign-in routes themselves (none found in `auth/api.py`), terms or privacy pages (none found).

**Frontend today.** One app serves both modes. On start it calls `/api/auth/session` (`frontend/src/pwa/offline.ts:249`): 200 means signed in to hosted, 404 means local, 401 means hosted and signed out (`offline.ts:253-276`). Signed out, the shelf shows a plain "Sign in" link to `/api/auth/login?return_to=/` (`frontend/src/shelf/Shelf.tsx:523`); there is no sign-in screen. `api.ts` sends no credentials header: the browser sends cookies, and every changing call adds `X-CSRF-Token` from the CSRF cookie (`frontend/src/api.ts:366-372`). The AI-key screen is `frontend/src/shelf/ProviderSettings.tsx`. The companion calls (`/read-map`, `/ask/stream`, `/conversation`, `/api/reader/...`) are probed and fall back on 404 (`api.ts:778-799`, `frontend/src/reader/ReaderPage.tsx:308,502`), so the frontend already copes with hosted lacking them.

### B3. Storage

- **Schema.** 17 forward-only migrations in `backend/app/hosted/schema/0001_...sql` to `0017_...sql`. Every tenant table has `owner_id` in its primary key and a row-level security policy on `app_current_owner_id()` (`schema/0004_prefilter_and_rls.sql:2-8,64-243`; later tables add their own, e.g. `0016:24-28`).
- **Migrations.** `python -m app.hosted.migrations` reads `DATABASE_URL`, applies each pending file in its own transaction under an advisory lock, records a checksum, and refuses gaps, edited files and transaction statements (`backend/app/hosted/migrations.py:32-55,58-113`). **Version numbers must be contiguous** (`migrations.py:51-54`): two parts adding `0018` in parallel will collide, so numbers must be handed out in advance. Database roles and grants are not in migrations; they are "provisioned outside migrations" (`backend/README.md`, section "Hosted OIDC and sessions"), and the only grant statements in the repository are in test fixtures.
- **Book files.** Uploaded EPUBs go to object storage under the owner and an opaque id: encrypted filesystem (AES-256-GCM) or S3 with server-side encryption (`backend/app/hosted/storage.py:120,285,465`; settings `config.py:136-147`). Metadata in `source_objects` (`schema/0002...sql:30-48`).
- **Chapter text.** At upload the server segments the EPUB and stores each chapter's full text in `book_search_documents.content`, per owner (`tenant/api.py:549-564`, `schema/0016_owner_scoped_book_search.sql:2-19`). This is the hosted source for anything that needs chapter text.
- **Story memory.** There is no `memory.db` and no `store.book(...)` in hosted mode. The per-book memory is Postgres tables (`chapters`, `ingested_chapters`, `entities`, `aliases`, `edges`, `events`, `chunks`, `chunk_embeddings`, ..., `schema/0002...sql`), all per owner. They are filled only by the extraction worker (B5) or, on 17 November, by importing a local archive (`backend/app/hosted/local_migration.py`, removed on 18 November with the move from the local app, ADR 0043).
- **Covers.** Not found for hosted (no cover route in `tenant/api.py`; local has `/api/books/{id}/cover`, `api/books.py:164`).
- **Per reader versus shared.** Everything is per reader: two readers uploading the same EPUB get two books, two files, two memories. The only shared data is service metadata (`provider_capabilities`, `schema/0001...sql:64-71`). The classics catalogue would be shared, but classics are not in hosted mode.

### B4. What a hosted reader can do today

| Feature | Hosted | Evidence |
|---|---|---|
| Sign in, sign out | works, with an OIDC provider configured | `auth/api.py:81-250` |
| Import a book (upload EPUB) | works | `tenant/api.py:511-610`; the README's "upload unavailable" (`backend/README.md`, "Hosted OIDC and sessions") is out of date |
| Delete a book | works (soft delete plus file delete) | `tenant/api.py:755-778` |
| Read (open the EPUB, manifest) | works | `tenant/api.py:694-752` |
| Position saved | works, different shape: versioned cross-device merge; the client says which chapter is completed | `tenant/api.py:90-98,781-794,1521-1578`; `schema/0014...sql` |
| Start again (position reset) | works | `tenant/api.py:1558` |
| Highlights, notes, bookmarks, page look | works (hosted only; local mode has none of these routes) | `tenant/api.py:797,1267-1519` |
| Search in the book | works, bounded by the chapter counter | `tenant/api.py:815`, `repository.py:334` |
| The older reply path (`/ask`) | partly: works with the reader's own key for "synthesis" and "judge"; passages come from full-text search, bounded by the chapter counter, not the read map; no streaming, no Jev, no stored turns, earlier conversation comes from the browser | `tenant/api.py:937-1052`, `repository.py:389-477` |
| Selection help, chapter wrap-up | partly: same reader-key path | `tenant/api.py:1055,1162` |
| Cast, graph, character, timeline, notes view | absent (local `views.py` routes not mounted). `/memory` and `/memory-corrections` exist but read tables nothing fills, unless a local archive was imported | `tenant/api.py:1581-1641`; B5 |
| Recap ("catch me up") | absent | local only, `api/views.py:1497` |
| Free library (classics) | absent | router not mounted (`main.py:500` local only) |
| Reading history imports | absent | router not mounted (`main.py:501`) |
| Costs view | works, per owner | `tenant/api.py:1643` |
| Background jobs list and cancel | works, but jobs never run (B5) | `tenant/api.py:631-670` |
| Moving the owner's local library up | absent: the command and its runbook were removed on 18 November (ADR 0043, D3) | was `hosted/local_migration.py:969`, `docs/runbooks/local-to-hosted-migration.md` |

### B5. The gap

Background work in hosted mode: there are **no in-process threads**. Upload inserts a row in `jobs` (`repository.py:1890,1962`), `pending` if the reader has a ready key, else `waiting_configuration`. A separate process, `mothli-hosted-worker` (`backend/pyproject.toml:31`, `backend/app/hosted/worker.py:116-146`), claims jobs with leases and heartbeats from Postgres (`jobs.py:281`) and calls a handler named by `HOSTED_JOB_HANDLER`. **No handler exists in the repository** (`worker.py:129-130` requires one; ADR 0024 lines 83-88, 104-106 says production must not start the worker without it). So hosted books are never extracted.

**Tables** (local definitions in `backend/app/catalog/schema/catalog.sql`):

| Local table | Line | Hosted |
|---|---|---|
| `books` | 12 | yes, different shape: `owner_id`, UUID ids, soft delete (`schema/0002...sql:2-26`) |
| `reading_state` | 29 | yes, different shape: plus offsets and a version clock (`0002:53-66`, `0014`) |
| `conversation_frontier` | 39 | different shape: `reading_state.current_cfi/current_offset/high_water_offset` (`0014`) |
| `cost_ledger`, `cost_reservations` | 50, 66 | yes, different shape: per owner; `book_id` may be NULL; `phase` limited to `extraction, synthesis, embedding, judge` (`0003...sql:109-166`, check at 116 and 143) |
| `home_cost_ledger`, `home_cost_reservations` | 86, 98 | no (hosted `cost_ledger` with `book_id` NULL could hold home spend) |
| `chapter_notes` | 116 | no |
| `conversation_turns` (with `drew_on`) | 133 | no |
| `read_ranges` | 152 | no |
| `skimmed_ranges` | 166 | no |
| `read_position` | 178 | no |
| `recollections` | 192 | no |
| `recollection_cursors` | 208 | no |

**Catalog methods** (`backend/app/catalog/catalog.py`):

| Method | Line | Hosted |
|---|---|---|
| `get_conversation_frontier`, `set_conversation_frontier` | 265, 272 | different shape: `get_position`/`update_position` (`repository.py:796,1104`) |
| `set_chapter_notes`, `get_chapter_notes` | 300, 309 | no |
| `add_turn`, `get_turns`, `clear_turns` | 316, 334, 449 | no |
| `add_read_range`, `get_read_map`, `clear_read_map` | 361, 380, 420 | no |
| `add_skimmed_range`, `get_skimmed` | 391, 409 | no |
| `set_read_position`, `get_read_position` | 434, 442 | no |
| `get_recollections`, `keep_recollections`, `touch_recollections`, `delete_recollection`, `clear_recollections`, `get_recollection_cursor` | 458-551 | no |
| `reset_position` | 553 | yes (`repository.py:1219`) |
| `record_cost`, `reserve_cost`, `settle_cost`, `note_reservation_actual`, `discard_cost_reservation`, `total_cost`, `get_costs` (with the home ledger behind `HOME_SPEND`) | 651-865 | different shape: `reserve_provider_call`, `settle_provider_call`, `list_costs` (`repository.py:588,707,1765`), owner-wide, no per-book ceiling |

**Endpoints** (local routers; hosted equivalents in `tenant/api.py`):

| Local endpoint | Where | Hosted |
|---|---|---|
| `PUT /api/books/{id}/read-page` | `api/reading.py:127` | no |
| `PUT /api/books/{id}/read-chapters` | `api/reading.py:161` | no |
| `GET /api/books/{id}/read-map` | `api/reading.py:96` | no |
| `GET /api/books/{id}/read-offer` | `api/reading.py:109` | no |
| `GET/PUT /api/books/{id}/read-frontier` | `api/reading.py:22,37` | no |
| `GET/PUT /api/books/{id}/position`, `POST .../position/reset` | `api/reading.py:209,223,262` | different shape (`tenant/api.py:781,1521,1558`) |
| `POST /api/books/{id}/ask/stream` | `api/views.py:574` | no |
| `GET/DELETE /api/books/{id}/conversation` | `api/views.py:602,629` | no |
| `POST /api/books/{id}/ask` | `api/views.py:642` | different shape (`tenant/api.py:937`) |
| `POST /api/reader/ask/stream` (home chat) | `api/profile.py:72` | no |
| `GET/DELETE /api/reader/conversation` | `api/profile.py:87,96` | no |
| `GET /api/reader/profile` | `api/profile.py:107` | no |
| `GET/DELETE /api/reader/recollections`, `DELETE .../{id}` | `api/profile.py:117,126,134` | no |
| `/api/classics...` | `api/classics.py:89-154` | no |
| `/api/history...` | `api/history.py:102-249` | no |
| `/people`, `/graph`, `/character`, `/timeline`, `/notes`, `/catch-me-up` (`/prepared-questions` was removed on 18 November, ADR 0043 R4) | `api/views.py` | no |

**Background workers:**

| Worker | Local | Hosted |
|---|---|---|
| Extraction, `IngestWorker.enqueue` | in-process thread pool of 2 (`backend/app/ingest/worker.py:141,150,163`) | durable job table and separate worker process, but no handler (above) |
| `NotesBuilder` | one thread, a set of queued book ids, one pause for the whole process (`backend/app/chapter_notes.py:136-177`) | no |
| `Recollector` | subclass of `NotesBuilder` (`backend/app/recollections.py:416`) | no |
| Recap pre-warmer | in-process (`main.py:433-441`) | no |

**Cost ledgers and ceilings.**
- Local: per book, by tokens and dollars (`config.py:110-116`, `cost/limits.py:229-231,288-290`); the home chat is charged to the first book on the shelf, or to `HOME_SPEND` when the shelf is empty (`home_conversation.py:363`, `recollections.py:440-444`).
- Hosted: per owner. One lifetime dollar cap `owner_limits.max_spend_usd`, **NULL (no cap) by default** (`schema/0011_owner_limits.sql:13-14`), checked atomically with a concurrency limit of 2 calls at once (`repository.py:604-680`). Per call, the input token ceiling applies (`provider_runtime.py:59-68`). There is no per-book, per-day or per-month ceiling in hosted mode. The spend is on the reader's own provider account (`tenant/api.py:853`).
- The local-to-hosted migration carried `books`, `reading_state`, `cost_ledger` and memory, not the new tables (`local_migration.py:292,628`; per-book archive tables `lifecycle/archive.py:43`). It was removed on 18 November (ADR 0043, D3).

### B6. Models and money in hosted mode

- **Provider.** Each reader stores their own key (`POST /api/credentials`, `tenant/api.py:344`) and picks a provider and model per capability, `extraction`, `synthesis`, `embedding`, `judge` (`backend/app/hosted/provider_settings.py:12-15`), from `openai-compatible`, `anthropic` or `offline` (`tenant/api.py:220`). Recommended defaults name `gpt-6.1-terra` and `text-embedding-3-small` (`provider_settings.py:18-35`). Allowed provider addresses: `HOSTED_PROVIDER_ALLOWED_ORIGINS` (`config.py:153-155`). Keys are decrypted just in time with `HOSTED_CREDENTIAL_MASTER_KEY` (and `HOSTED_CREDENTIAL_PREVIOUS_MASTER_KEYS` during rotation), `config.py:150-152`. Removed on 18 November: readers give no key and the platform pays (ADR 0043, D1); `HOSTED_PROVIDER_ALLOWED_ORIGINS` stays, for the platform writer's address.
- **The operator's own keys** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`, `config.py:24-37`) are not used anywhere in the hosted lifespan (`main.py:347-391`). There is no operator-paid path.
- **Jev** (the TypeSafe checker every companion sentence goes through) is not referenced anywhere under `backend/app/hosted/`. Someone has to pay for it per sentence: no reader key covers it today.
- **`codex-local`** is refused outside local mode (`config.py:184-185`).
- **At a ceiling.** Over the owner cap or concurrency: HTTP 409 or 429 with `code`, `limit`, `retry_after_seconds` and an `action` (`tenant/api.py:295-313`). Over the per-call token ceiling: 429 (`tenant/api.py:1008-1009`). No key or an unvalidated one: 503 `ai_not_configured` / `provider_not_ready` (`tenant/api.py:868-885`). In the worker, a cap makes the job fail as `budget_exceeded`, not retried (`worker.py:71-79`).

### B7. Deployment

| Piece | State |
|---|---|
| Dockerfile, compose, platform config (Fly, Render, Kubernetes, ...) | none found anywhere in the repository |
| Process commands | web: `uvicorn app.main:create_app --factory` (`README.md:100`); worker: `mothli-hosted-worker` (`pyproject.toml:31`); migrations: `python -m app.hosted.migrations`; key rewrap: `mothli-credential-rewrap` (removed on 18 November, ADR 0043 step 8); limits and audit: `python -m app.hosted.limits`, `python -m app.hosted.audit` |
| Environment variable list | in code (`config.py:118-156`) and `backend/README.md`; `.env.example` covers local only |
| Database roles and grants | described in ADR 0020 and `backend/README.md`; no script. Four roles are expected: auth (`HOSTED_AUTH_DSN`), tenant (`HOSTED_TENANT_DSN`), worker (`HOSTED_WORKER_DSN`), audit (`HOSTED_AUDIT_DSN`) |
| Health | hosted has only `GET /api/health/live` (`main.py:485-487`); no readiness check (local has `/api/health/ready`, `api/health.py:17`) |
| Static frontend | served by the backend **only in local mode** (`main.py:506-509`). Hosted needs something else to serve `frontend/dist` on the same host as `/api`, because the cookies are `__Host-` and SameSite=Lax |
| HTTPS and hosts | cookies are Secure (`auth/api.py:58`); `TRUSTED_HOSTS` must be explicit (`config.py:189-191`); OIDC URLs must be HTTPS (`config.py:209-217`). TLS itself is left to whatever sits in front |
| Security headers | applied in both modes (`main.py:41-84`); API responses marked no-store in hosted (`main.py:234-252`) |
| Logging | plain Python logging; content-free by design (`audit.py`, `main.py:87-106`); no shipping or metrics found |
| Backups | local only (`hosted/local_migration.py:936` on 17 November, removed with the move from the local app, ADR 0043; ADR 0009); for Postgres and object storage, none: "Backups, restore drills, deployment configuration, monitoring ... remain operator responsibilities" (`README.md:10-13` on 17 November; that sentence went when the README was rewritten for one hosted product, ADR 0043) |
| Local run script | none: `scripts/run-local-production.sh` was local mode only, and was removed on 18 November (ADR 0043, D4) |

### B8. Tests

- Hosted tests: `backend/tests/hosted/` (16 files) and `backend/tests/parity/` (SQLite and Postgres must give the same spoiler-bounded answers). The Postgres ones skip unless `TEST_POSTGRES_DSN` is set.
- CI: workflow "PostgreSQL schema" (`.github/workflows/postgres-schema.yml`), job `migrations`, runs a `pgvector/pgvector:pg16` service and `pytest backend/tests/hosted backend/tests/parity` with `TEST_POSTGRES_DSN` set (lines 11-45). So the migrations are really applied there, and the cross-tenant, role-privilege and job-lease tests really run. The main CI (`ci.yml:37-39`) runs the whole suite without Postgres, so those skip there.
- Locally: `backend/scripts/test_postgres.sh` starts the same image in Docker and runs the same tests. Docker is not on this machine's PATH.
- **Run here** (from `backend/`, no `TEST_POSTGRES_DSN`): `tests/hosted/test_migrations.py`: 1 passed, 18 skipped. `tests/hosted tests/parity`: 59 passed, 75 skipped, none failed. Everything that needs Postgres skipped.

### B9. Risks specific to many readers

1. **No reader key in any new local query.** Each needs `owner_id` in the key and in the query, plus a row-level security policy:
   - `chapter_notes`: `set_chapter_notes` (`catalog.py:304`), `get_chapter_notes` (`:312`).
   - `conversation_turns`: `add_turn` (`:327`), `get_turns` (`:342`, `book_id IS ?` with NULL meaning *the* home conversation), `clear_turns` (`:453`).
   - `read_ranges`, `skimmed_ranges`: `add_read_range` (`:369-377`), `get_read_map` (`:384`), `add_skimmed_range` (`:399-406`), `get_skimmed` (`:412`), `clear_read_map` (`:423-431`).
   - `read_position`: `set_read_position` (`:438`), `get_read_position` (`:444`), and the update in `clear_read_map` (`:430`).
   - `recollections`: `get_recollections` reads every row (`:464`); `keep_recollections` replaces by id and **enforces the 300 cap across the whole table** (`:482-506`); `touch_recollections` (`:516`) and `delete_recollection` (`:522`) **by integer id alone**; `clear_recollections` **deletes every row** (`:533-539`).
   - `recollection_cursors`: key `(conversation, incarnation)` with `''` for the home conversation (`catalog.sql:208-214`); `get_recollection_cursor` (`:548`), and the inserts in `keep_recollections` and `clear_recollections`.
   - `home_cost_ledger`/`home_cost_reservations`: one row set for the whole install, `book_id = 'home'` (`catalog.sql:86-111`).
   - Integer ids (`turn_id`, `recollection_id`) are guessable and SQLite reuses deleted ones (`catalog.sql:199-200` comment). `DELETE /api/reader/recollections/{id}` (`api/profile.py:126`) ported as it is would let one reader delete another's note. Hosted uses UUIDs and returns the same 404 for "not yours" and "missing" (ADR 0021); the port should too.
2. **Same book, two readers.** Local turns, read map and notes are keyed by `book_id` (plus `incarnation`). In hosted each upload gets its own `book_id` (`tenant/api.py:531`), so two readers do not share a key unless someone adds de-duplication; keep it that way, or the read map and turns would merge. Chapter notes are a function of the chapter text (`catalog.sql:113-115`), so sharing them between readers of the same file would save money; that is decision 8 and must never share turns, recollections or the read map.
3. **Shared files.** Local classics and covers are files under one `DATA_DIR` (`api/classics.py:55`). In hosted they must be per owner or proven content-free. (The answer bank and recap file cache that were also here were removed on 18 November, ADR 0043 R3 and R5.)
4. **Per-process background state.** `NotesBuilder` keeps its queue in a Python set and its five-minute pause in one attribute for the whole process (`chapter_notes.py:148-177`). With many readers on their own keys, one reader's refused key would pause every reader's notes; with two web processes, two builders could write the same notes, and the pause and queue are lost on restart. ADR 0017 (lines 149-154) already requires shared state for locks and quotas. The hosted lock registry is also per process (`backend/app/hosted/runtime.py:76`); ADR 0022 accepts that only as an optimisation.
5. **Per-book ceiling.** The local ceiling is per book; the home chat is charged to the first book and lifted when that book is removed (open items 150-151). In hosted the only cap is per owner and unlimited by default. A shared book (risk 2) with a per-book ceiling would let one reader exhaust another's allowance; a per-owner cap avoids that.
6. **The home chat reads "every book".** `home_conversation.py` and `recollections.py` call `catalog.list_books()` and read maps of all books; in hosted that must be the owner's books only, or the home chat would read other readers' shelves.
7. **Jev and the writer are one account in local mode.** In hosted, whoever pays the checker sees every reader's sentences; that needs to be in the privacy policy.

### B10. Decisions only the owner can make

| Decision | Options visible in the code | What the code assumes now |
|---|---|---|
| Where it runs | Any host with Postgres 16 plus pgvector, an S3-compatible bucket or a disk, HTTPS in front, one web process plus one worker process | Nothing chosen; `pgvector/pgvector:pg16` in CI |
| Sign-in | Any one OIDC provider: Google, Apple, Microsoft, or a broker (Auth0, Clerk, Keycloak, ...) that offers email and password and several social logins behind one issuer | One issuer, no passwords, sessions of 8 hours, signed out after 30 idle minutes (`config.py:131-132`) |
| Who pays for AI | Reader's own key; operator key with caps; free allowance then reader key or a subscription | Reader's own key only (`tenant/api.py:853`); no Jev at all |
| Budget per reader | Per-owner lifetime cap exists; per-day or per-month and per-book do not | No cap (`max_spend_usd` NULL) |
| Limits on free use | Per-owner numbers in `owner_limits` | 100 books, 5 GB, 128 MB per upload, 3 active jobs, 600 requests a minute, 2 AI calls at once (`schema/0011...sql:4-14`) |
| Data on deletion | Immediate hard delete; soft delete with a grace period; how long backups and the audit log keep it | Books soft-deleted plus file deleted (`tenant/api.py:755-778`); no account deletion; audit purge 90 days (`backend/README.md`) |
| Terms and privacy | Must name the AI providers and the checker that see reader text | None in the repository |
| Shared chapter notes | Per reader, or shared per identical file | Per reader |
| Session length | Longer sessions for a reading app | 30 minutes idle |
| Moving the owner's own local library | `local_migration.py` and its runbook, both removed on 18 November: the owner needs nothing from the local copy (ADR 0043, D3) | Carried books, position, ledger, memory; not notes, turns, read map or recollections |

### B11. The parts

Shared files to watch: `backend/app/hosted/tenant/api.py`, `backend/app/hosted/tenant/repository.py` (including the allow-list at `:57-85`), `backend/app/hosted/tenant/endpoints.json`, the test fixture grants in `backend/tests/hosted/test_tenant_postgres.py`, `backend/app/config.py` validation, `backend/app/main.py` lifespan, and the migration numbers. Part 1 removes most of the first three as a source of conflicts.

| # | Part | Delivers | Files | Depends on | Tested without real models | Size | Owner must decide first |
|---|---|---|---|---|---|---|---|
| 1 | Split and number | `tenant/api.py` and `repository.py` split into one router and one repository module per feature (library, position, marks, credentials, ask, ...) with no behaviour change; a written list of migration numbers per part; the allow-list built from the modules | `hosted/tenant/*`, `endpoints.json`, `tests/hosted/test_tenant*.py` | none | existing hosted tests unchanged, in CI with Postgres | small | nothing |
| 2 | **Proof: read map in hosted** | Migration `0018`: `read_ranges`, `skimmed_ranges`, `read_position` with `owner_id`, UUID book and incarnation keys, row-level security; an owner-bound **sync** adapter (`HostedCompanionStore(owner_id)`, on the `jobs.py` pattern) with `add_read_range`, `get_read_map`, `add_skimmed_range`, `get_skimmed`, `clear_read_map`, `set_read_position`, `get_read_position` under the same names as `Catalog`, so `read_map.py` runs unchanged; routes `PUT /read-page`, `PUT /read-chapters`, `GET /read-map` reading chapter text from `book_search_documents`; grants, allow-list and inventory entries; a settings switch so the read map can be on in hosted without the rest | new `hosted/schema/0018_...sql`, new `hosted/companion_store.py`, new `hosted/tenant/reading_map.py` (router), `endpoints.json`, `config.py` (one check), `main.py` (include router), tests | 1 | Postgres test with two owners: A records pages, B sees an empty map and gets 404 for A's book; RLS-bypass run as in ADR 0021; `read_map.py` unit tests against the adapter; check that `normalize_source` over `book_search_documents.content` gives the same offsets as the local chapter text (not verified here) | medium | nothing |
| 3 | Conversations inside a book | `conversation_turns` with `owner_id`, UUID `turn_id`, `drew_on`; `add_turn`, `get_turns`, `clear_turns` on the adapter; `GET/DELETE /conversation` | `0019`, `companion_store.py`, new router module, tests | 2 | two owners, turns hidden after "read it fresh", stand-in writer not needed | medium | nothing |
| 4 | Book text and story memory in hosted | A hosted stand-in for `store.book(book_id)` and for the worker's chapter texts and manifest, read-only over Postgres (`chapters`, `entities`, `chunks`, `book_search_documents`); decide whether the stream's safety check (`stream_conversation.py:383-391`) can run on an empty memory or must wait for extraction | new `hosted/book_source.py`, tests; no change to `stream_conversation.py` if the interface matches | 1 | parity-style tests: same answers from SQLite and Postgres for the calls the companion makes | medium | nothing |
| 5 | Paying for the companion | Writer and Jev clients per owner or per operator; ledger phases for reply, second look, notes, recollections, home (migration widening the `phase` check at `0003:116,143`); the budgeted call wrappers (`cost/limits.py:194` `budgeted_completion`, `llm/jev_budget.py:13` `budgeted_jev`) backed by the hosted reservation; per-day and per-book ceilings if chosen; home spend with `book_id` NULL | `0020`, `hosted/provider_runtime.py`, new `hosted/companion_spend.py`, `config.py`, `provider_settings.py`, tests | 1; decisions | stand-in clients (as the local suite does), Postgres tests for concurrent reservations at the cap | medium to large | who pays, caps, which models |
| 6 | Streamed chat in a book | `POST /ask/stream` in hosted using parts 2 to 5; the settings stop refusing the companion in hosted (`config.py:170-172`) | new router module, `config.py`, `main.py`, tests | 2, 3, 4, 5 | the local stream tests run against the hosted adapter with stand-in models; two-owner test | large | 3, 4 |
| 7 | Background work for many readers | `NotesBuilder` and `Recollector` as durable job kinds on the existing `jobs` table and worker, pause per owner or per key, not per process; the missing extraction `HOSTED_JOB_HANDLER`; chapter notes table and reading | `0021`, `hosted/jobs.py`, new `hosted/handlers.py`, `chapter_notes.py` and `recollections.py` only if their build functions need a seam, tests | 4, 5 | worker tests as in `test_worker.py`, two owners with one refused key: only that owner pauses | large | shared notes (decision 8) |
| 8 | Home chat, recollections, suggestions | `recollections`, `recollection_cursors` per owner with UUIDs; the cap per owner; `/api/reader/...` routes; `list_books` per owner | `0022`, `companion_store.py`, new router module, tests | 3, 5, 7 | two owners: B cannot list, delete or cap-evict A's notes | large | data on deletion |
| 9 | Deployment | Dockerfiles for web and worker, a migration step, a role-and-grant script, a readiness route, static frontend on the same host, log shipping, Postgres and bucket backups with a restore drill | new files at repo root and `deploy/`; `main.py` only for readiness | none | build the images; run the Postgres suite inside them | medium to large | where it runs |
| 10 | Free library and history import | `/api/classics` and `/api/history` in hosted, books going through the hosted upload path and object storage; history in per-owner tables | `0023`, `0024`, new router modules | 1 | two owners; Gutenberg fetch replaced by a fixture | medium each | limits on free use |
| 11 | Accounts and the reader's screens | delete and export account; sign-in and first-run screens; the AI setup screen or its removal; terms and privacy pages | `hosted/auth/*`, new router module, `frontend/src/shelf/*`, `frontend/src/pwa/offline.ts` | 1; decisions | backend: deletion removes every owner row and object; frontend tests with the API mocked | medium | sign-in, who pays, deletion, terms |

**Can run in parallel.** After part 1: parts 2, 4, 9 and 10 touch different files and can run at once. Part 9 can start before part 1. Part 11's backend can start after part 1 once decisions are in; its frontend can start any time. Part 5 can run beside parts 2 and 4 once its decisions are made. **Must be in order:** 2 before 3; 3 before 8; parts 2 to 5 before 6; 4 and 5 before 7; 7 before 8. Every part that adds a migration takes the number assigned in part 1.

### B12. What I could not determine

- Whether `normalize_source` over `book_search_documents.content` gives exactly the chapter texts the local read map uses (both come from `segment_for_ingest` at first sight; not compared).
- Whether the streamed reply's checks work with an empty story memory, i.e. before any extraction in hosted.
- Whether the hosted upload, position and marks routes work against a real Postgres today: those tests skip here and Docker is not available; CI runs them.
- How the frontend behaves end to end against a hosted server: not run.
- What the four database roles' exact grants should be in production beyond ADR 0020 and the test fixtures.
- What a real reader's companion spend per day would be on paid models: every measured run so far used `codex-local` for the writer.
