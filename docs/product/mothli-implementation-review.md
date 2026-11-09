# Mothli conversation implementation and verification

## Reader behavior and data

The reader has Contents, Notebook, Mothli and compact settings. Conversations and saved Mothli
replies are browser data scoped to owner, book and reading pass; they are cleared on sign-out.
They are not server-synced. Clearing chat does not delete explicitly saved replies. Existing source
files, positions, marks, annotations and correction data remain intact.

## Source and spoiler contracts

- General conversation receives only a verified primary-text prefix, independent of extraction
  progress. Snapshot identity binds book incarnation, source/atom hashes, reset epoch and exact
  offset. File paths use validated book IDs and resolved storage-root containment, rejecting
  symlink escapes before source hashing. Stale, forged or changed-source boundaries fail before inference. An admitted older
  boundary remains narrow while a page turn is in flight. Reading progress is a monotonic high-water
  mark; moving backward does not authorize an unseen suffix or reset the reading pass.
- Selected passages are a separate explicitly authorized scope: only the exact reader-supplied
  selection is sent, after membership/location checks. No surrounding unread chapter body is added.
  Selection history is reference context, not evidence. This permits a visible selection before
  chapter extraction finishes; it does not authorize using the rest of that chapter.
- Recent conversation may resolve references but never proves a book fact. Retrieval keeps
  conflicting premises eligible. Every answer claim retains its own citations; another claim's
  proof, a chapter title or pretrained plot knowledge cannot fill a gap.
- Drafts remain private. Positive proof, semantic checks, fixed source references and spoiler
  checks must pass before publication. Uncertainty is not approval. SDK truncation, malformed
  output or content-filter stops never display partial drafts.

## Coordination

1. Validate source and reading snapshot, then classify the turn with typed Jev judgments.
2. Exact bounded arithmetic runs in code. A recognized calculation followed by an explicit second
   request is separated: the remaining book question still follows ordinary evidence checks.
   Computation has no book citations and cannot mark an unanswered book question as answered.
3. Brief catch-ups use bounded sentence-aligned exact tail slices. Proven overlaps coalesce in
   chronological order; gaps stay separate. Other questions keep hybrid retrieval and bounded
   source preparation. Sources are clipped before retrieval, prompting, scoring and caching.
4. Terra drafts concise prose. A scene catch-up may include one private, simpler alternative from
   the same call. Both are checked against their own sources. A separately safe alternative is
   still eligible when the main draft fails validation, but must retain its standalone-meaning gate.
5. Jev supplies narrow support, qualification, place, timing, cause and usefulness judgments.
   Ordinary factual/multipart requests also require whole-request coverage. Rejections or uncertain
   coverage receive a full independent semantic review, not selective proof of only one detail.
6. If reasoning is necessary for a catch-up, prefer the simpler complete candidate when eligible.
   Terra receives no Jev scores and must prove every selected claim and its independent meaning.
   A verified main prefix or the exact verified alternative may publish; unchecked fragments may not.
   Requests batch within Jev's 24-question limit without dropping checks.

The people shortcut reads the bookmark-clamped graph, or asks Jev to classify bounded exact-source
name candidates when memory is cold. It does not generate a recap. The active UI does not call legacy
whole-recap or chapter-closeout routes; those remain compatibility APIs. Definitions/explanations
retain Terra review; changed translations may clear a tested Jev gate with Terra fallback.

## Caching, lifecycle and defaults

Reviewed answer keys are four-part `(book, incarnation, policy/source scope, question)` tuples.
The scene namespace lives inside the scope digest so persistence remains compatible. Source, epoch,
model and review-policy changes invalidate reuse. Contextual replies are not persisted as general
answers; an explicitly history-independent catch-up has its own scoped entry. Only checked answers
are cached, never drafts or personal conversation to manufacture cache hits.

Read-only chat, people suggestions and preparation share lifecycle leases. Reset, delete and
correction mutations wait for active reads. Reader position persistence must finish before admitting
an exact page anchor, including the trailing latest report after a far jump. Backend admission guards
remain authoritative; UI ordering must not relax them.

The Jev conversation experiment requires explicit local page-context settings. Production defaults
retain the existing guarded pipeline and automatic memory ingestion. The local test launcher uses
native Terra API inference, Jev 1.14, low draft/review effort, a shared $0.25 session ceiling and a
30-second provider timeout; it disables automatic extraction backlog/prewarming. No premium Fast
service tier or Codex subprocess is used by that launcher. Credentials stay outside the repository.
The API transport, legacy local Codex transport and old endpoints retain their compatibility contracts.

## Measured evidence and limits

Historical native Terra baseline versus the first fused/parallel Fast experiment: median 15.206 s
versus 4.724 s over 12 requests each; Fast pricing and generation variance were not isolated. Later
standard Terra/Jev source preparation is the retained local path. Three repeated full-end catch-ups
measured 5.78 s median before the source/alternative changes versus 2.81 s after them. Median Terra
input fell 6,482 to 2,635 tokens and output 273 to 90. Standard Terra token-rate estimate fell about
61%, excluding Jev and provider cache discounts/write surcharges. These small samples are not p95s.

Cold QA covered 26 cases with extraction progress zero: unopened books, incremental pages, chapter
completion, immediate middle/end jumps, selections, compound questions, forged history, fabricated
selections and stale/future boundaries. All expected outcomes passed; no unread canaries reached
provider payloads. Early-page novel checks were about 2–4 s; cold novel middle/end jumps 2.93/2.37 s.
Fifteen unsupported factual controls were withheld, including invented identity, settings, causes,
attribution, certainty and outcomes. Conservative checks also withheld some supported controls.

Seventeen additional unusual questions covered reactions, contextual references, disagreement,
false premises, translation/explanation, arithmetic plus book facts, another novel, nonsense and
Spanish. The retained replay had no server errors: median 2.489 s, range 1.983–6.051 s. Some responses
correctly lacked evidence; HTTP 200 does not imply a substantive answer. The 9.220-second mixed-math
failure was fixed by separating calculation from book proof. More helpful partial-answer prompt
wording caused inconsistent structured output and was rejected. Source-layout-only and fresh-repair
experiments also failed to establish reliable gains and were not retained as complete solutions.

The historical 8.745-second failure retained timings, not rejected text, so its exact wording cannot
be reconstructed. Four later catch-up probes passed in 2.523–3.418 s with one Terra call each. Fresh
running-app checks passed at 3.688 s for catch-up and 3.613 s for arithmetic plus an Alyosha identity
question. Controlled independent verification rejected an unsupported setting, cleared a simpler
sentence and rejected an invented resurrection. Uncertain future replies can still require slower
reasoning or fail closed; there is no instant-response guarantee or production error-rate claim.

## Pre-merge review

Three independent reviewers examined safety, architecture/concurrency and API/frontend behavior.
Confirmed issues: recap cache persistence, exclusive people-list locking, page-jump/anchor ordering,
unsafe-primary suppression of a safe alternative, and missing whole-request completeness review.
All received focused regressions and independent rechecks with no unresolved blockers. Local
backend validation passed 1,842 tests with 83 skipped; frontend tests/build, Ruff and Bandit passed.
Final CI results are recorded with the pull request; the existing local verification JSON retains detailed
measurements. Disposable intermediate report content was compacted instead of adding reports.

Reproduce measurements with the existing `backend/scripts/benchmark_*` scripts using explicit
isolated input/data directories and small spend ceilings. Local verification used shared Python 3.13
and Vitest 3.2; CI validates locked Python 3.12 and Vitest 4 dependencies, PostgreSQL parity, dependency
and secret scans. This is not a deployed or physical-device acceptance run.

References: [TypeSafe citation checks](https://docs.typesafe.ai/cookbooks/citation_check.md),
[TypeSafe model limitations](https://docs.typesafe.ai/go/jaggedness.md),
[OpenAI latency guidance](https://developers.openai.com/api/docs/guides/latency-optimization),
[prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).
