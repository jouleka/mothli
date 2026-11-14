# Mothli

Mothli is a self-hosted EPUB reader that builds a structured memory of the story as you
read. It helps you remember characters, relationships, events, and where you stopped without showing
material beyond your saved reading frontier.

**Mothli** is both the app and its AI reading companion. The repository contains the FastAPI service, React reader, local
SQLite story-memory engine, and an experimental hosted multi-user foundation.

> **Project status:** the local, single-user application is usable and covered by automated tests.
> The hosted mode is an incomplete engineering foundation, not a ready-to-deploy public service.
> Backups, restore drills, deployment configuration, monitoring, and an independent security review
> remain operator responsibilities.

## Features

- Import and read DRM-free EPUB files you are allowed to use. FB2, MOBI and AZW3 files (zipped or not)
  are converted to EPUB in the browser first.
- The Library: a page of about 77,000 public-domain books from
  [Project Gutenberg](https://www.gutenberg.org/) with their cover art. Browse shelves, pick a mood or
  a genre, search by title or author, read a description, and add a book with a tap. The list is
  every text Project Gutenberg marks public domain in the United States, built from its offline
  catalogue feed (`backend/scripts/build_classics_catalog.py`); readers elsewhere are told to check
  their own country's rules, and `CLASSICS_MIN_YEARS_SINCE_DEATH` narrows the list to a death-date
  rule if you need one. Covers are fetched once from Project Gutenberg's mirror and kept in the data
  directory. A book is downloaded only when a reader adds it; set `CLASSICS_ENABLED=false` for an
  offline install.
- Reading history: bring your list, ratings, reviews, highlights and notes from other reading apps by
  dropping in the files they export (Goodreads and StoryGraph libraries, Kindle `My Clippings.txt`,
  Libby exports). Files are read in the browser; titles are matched to the free Library so a book
  can be added with a tap. Copy-protected books are never opened or converted.
- Maintain a chapter-bounded, bitemporal story memory.
- Catch up and discuss people or passages in one continuing Mothli conversation.
- Keep highlights, notes, bookmarks, and explicitly saved Mothli replies in Notebook.
- Ask questions about completed chapters and explain text explicitly selected in the reader.
- Read aloud with browser speech synthesis.
- Install the reader as a PWA and queue reading-position changes while offline.
- Use SQLite and `sqlite-vec` locally; exercise PostgreSQL/pgvector parity in the hosted test suite.

## Spoiler-safety model

Derived facts are stamped with the chapter that revealed them. Reader-facing queries are constrained
to the server-authoritative bookmark, and the retrieval/evaluation suites contain adversarial
no-look-ahead checks. This is a defense-in-depth design, not a promise that model output can never be
wrong; keep the deterministic gates and regression tests enabled when changing ingestion or views.

New chapter memory also requires observed names and a source-bounded semantic review before commit.
The review must cover every memory field with numbered source evidence; prose fragments must retain
the complete original wording so an unsupported location, time, or causal link cannot be omitted from
the audit. Incomplete or invalid audits withhold the entire chapter. Evidence validation enforces the
review contract; semantic support remains a model judgment.
Ask and chapter closeout review each claim against its own citations. Existing saved memory is not
retroactively cleared by these checks. Completing the final visible page requires the explicit
**Finish book** action; merely displaying that page does not mark its text as read.

The original design is in
[`docs/spec/2026-06-25-reading-companion-design.md`](docs/spec/2026-06-25-reading-companion-design.md),
and accepted architecture decisions are recorded under [`docs/adr`](docs/adr).

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Node.js 20+ and npm
- A modern browser
- Optional: an OpenAI-compatible or Anthropic API key
- Optional: PostgreSQL 16 with pgvector for the hosted/parity suite

## Quick start

Clone the repository, create a local configuration, and install locked dependencies:

```bash
git clone https://github.com/jouleka/mothli.git
cd mothli
cp .env.example .env

cd backend
uv sync --extra dev --frozen

cd ../frontend
npm ci
```

Start the API in one terminal:

```bash
cd backend
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Start the CSP-enabled development frontend in another:

```bash
cd frontend
npm run dev
```

Open <http://localhost:5173>. The example configuration uses the deterministic offline stub, so you
can explore without a provider key. Add your own key to the ignored `.env` file and set
`ALLOW_STUB=false` when you want real extraction. Never commit that file.

The library and companion show whether AI is in demo mode, configured, connected, or unavailable.
A ready server using the offline stub does not establish working AI or correct answers. See the
[backend acceptance sample](backend/README.md#provider-status-and-live-acceptance-sample) before
evaluating a new provider configuration.

### GPT-6.1 Terra and local ChatGPT testing

The default OpenAI completion model is `gpt-6.1-terra` for both roles. Extraction, passage selection,
and question planning use low reasoning effort; answer/recap synthesis and structured evidence,
identity, and spoiler reviews use medium. Other explicitly configured models retain their profiles.

For local testing through your ChatGPT account, use the Codex CLI (tested with 0.167.0), run
`codex login` and verify `codex login status` reports ChatGPT. Use a separate local configuration:

```dotenv
LLM_TRANSPORT=codex-local
LLM_CHEAP_MODEL=gpt-6.1-terra
LLM_LARGE_MODEL=gpt-6.1-terra
ALLOW_STUB=false
EMBED_PROVIDER=stub
READER_PAGE_CONTEXT=true
READER_CONVERSATION_PIPELINE=true
READER_CONVERSATION_REVIEW_EFFORT=low
READER_ANSWER_CACHE=true
```

This uses the existing ChatGPT sign-in without copying its credentials into the project or browser.
Each completion runs an ephemeral CLI session in a temporary directory, with user configuration,
shell, plugins, and web search disabled. The existing validation and spoiler gates still apply.
The adapter is local-only, overlaps two foreground reviews, and gives background extraction a separate
single-call lane. It strips API-key environment overrides and has no fallback to API billing.
It requires a CLI version supporting `--ignore-user-config`, `--ignore-rules`, and `--ephemeral`.

Current-page conversation matches an exact EPUB body-text anchor and clips the source before
retrieval or model calls. Page turns confirm text before the current page; sending a question also
confirms the visible page. The UI says “Through this page.” It works before chapter extraction and
uses lexical retrieval plus recent source windows, with no embedding API calls. Ambiguous anchors
fail closed. Reading-pass resets invalidate old boundaries; backward turns retain already-read text.

ChatGPT completions do **not** provide embeddings. `EMBED_PROVIDER=stub` keeps new local test books
offline for embeddings; it does not make conversation generation a stub. Existing books retain their
pinned vectors and derived memory. Further chapter ingestion may require their original compatible
embedding provider or an explicit rebuild; current-page conversation does not wait for it. Keep API
keys in the production configuration, which the local subscription launcher must not load.

This testing adapter has CLI overhead and subscription limits. Its token ledger includes CLI usage;
USD entries for `chatgpt-plan:` are not API charges or a subscription price estimate. Unlike the API
transport, the CLI cannot enforce a provider-side output-token cap; excessive output is rejected
after completion. Use `LLM_TRANSPORT=api` and an API credential for normal API budget guarantees.

### Single-process local build

```bash
cd frontend
npm run build

cd ../backend
FRONTEND_DIST_DIR="$(pwd)/../frontend/dist" \
  uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 5173 \
  --no-proxy-headers --no-server-header
```

The application sets a restrictive Content Security Policy because EPUB markup is untrusted. Do not
serve the built frontend through a different proxy or static host unless it preserves equivalent
security headers.

## Companion foundations

Reading coordinates remain the original EPUB atoms, including illustrated pages (each retained
image-only atom reserves one position unit). The companion uses a separate content scope:
recognized supplementary headings such as acknowledgements, map lists and indexes, and empty
illustrated pages do not supply its recap, cast or search answers. Source text, completion receipts,
notes and backups retain their original coordinates. Introductions and unfamiliar headings remain
included. Heading recognition is deliberately conservative and currently uses English labels;
this is not a universal EPUB structure classifier.

`POST /api/books/{book_id}/ingest/retry` resumes preparation through the saved bookmark. It cannot
advance reading progress. Questions distinguish pending/interrupted preparation from unsupported
answers, and common recap requests reuse the reviewed recap flow. Content-policy versions scope
both recap and answer caches. Selected-text AI requests allow navigation/status reads while reset
and deletion still wait for their lifecycle lease.

Publisher chapter numbers are display metadata rather than replacements for internal ordinals.
The local ChatGPT transport can take several minutes to prepare long chapters; reading remains
available throughout. Its harness allowance is included in prompt splitting and cost reservations.

## Verification

```bash
cd backend
uv run ruff check .
uv run pytest

cd ../frontend
npm test
npm run build
npm audit --audit-level=high
```

The PostgreSQL workflow applies the real migrations and runs the hosted plus SQLite/PostgreSQL parity
suites against a pinned pgvector image.

## Repository layout

```text
backend/    FastAPI API, local memory engine, hosted foundation, and tests
frontend/   React/TypeScript reader, PWA support, and vendored Foliate renderer
docs/       Product design, ADRs, accessibility notes, and public runbooks
scripts/    Optional loopback-only local service helpers
spikes/     Reproducible research experiments and their synthetic/public fixtures
```

## Privacy and security

- Imported books, databases, `.env`, and generated data directories are ignored by Git.
- API documentation is disabled by default.
- The integrated server and Vite development server send browser hardening headers, including the
  CSP required by the vendored EPUB renderer.
- Hosted sessions are opaque, server-side, secure-cookie sessions with CSRF protection; hosted mode
  still requires environment-specific deployment review.
- Report suspected vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Public bug reports must use synthetic or public-domain
fixtures and must not include books, credentials, databases, or private infrastructure details.

## License

Original project code is available under the [MIT License](LICENSE). Vendored components keep their
own licenses; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and the license files beside
those components.
