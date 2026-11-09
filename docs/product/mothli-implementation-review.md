# Mothli implementation review — 7 November 2026

User reviewed the local implementation and approved merging on 7 November 2026. Implementation branch: `feat/companion-simple-shell`.

The reader now has Contents, Notebook, Mothli, and compact reading settings. The old Recap, People & sections, and Reflect destinations and their unused UI/test tree were removed. Existing book positions, marks, preparation, and backend correction data are preserved.

Mothli uses real requests, a bounded recent conversation, contextual passage questions, recap/person shortcuts, expandable sources, and explicit saves to Notebook. Model names, usage receipts, and technical status cards are absent from the reading flow. Short labels remain alongside unfamiliar icons. The bookmark mascot respects the quiet page without looping animation.

Conversation and saved Mothli replies are local browser data scoped to owner, book, and reading pass. They are cleared with the existing sign-out purge. They are not server-synced. Existing marks retain their previous hosted/offline behavior. The marks export contains marks, not saved Mothli replies. Clearing a chat does not delete explicitly saved replies.

Review repaired contextual-query length limits, prevented private contextual requests entering reusable answer caches, restored source-return paths, protected phone chat from inert background handling, and exempted desktop chat controls from reader arrow keys. Retained tests cover those meaningful boundaries and existing reading behavior; obsolete destination tests were removed.

Local validation: TypeScript/Vite build; 204 remaining frontend tests; 300 focused backend question, evidence, selection, cache, and route tests; full backend Ruff checks. Desktop and simulated phone browser checks covered opening/closing Mothli, a real Napoleon recap, saving it to Notebook, existing note navigation and return, and draft preservation after failed follow-up. This is not physical-phone keyboard validation or a deployed/hosted acceptance run.

The live follow-up revealed and fixed an internal 500-character validator mismatch. A later request exceeded the per-call input estimate, although the server mislabeled it as a book cost ceiling. Source-selection batching now retains every candidate within the existing cap. No account spend or book-budget exhaustion was established. The original Carlo follow-up returned HTTP 200 with checked citations after the repair (35.8 seconds on the local Codex transport). No cap was raised.

## Conversation pipeline — 8 November 2026, local and unmerged

Branch `codex/mothli-conversation-pipeline`, based on pulled main `1cbe79d`. Opt in with
`READER_CONVERSATION_PIPELINE=true` and `READER_CONVERSATION_REVIEW_EFFORT=low`.
The earlier API benchmark enabled native Terra `READER_FAST_MODE=true`; this has premium
pricing and retains every existing spend/token ceiling. Ingestion keeps its original model profile.

Retain hybrid lexical/semantic rank fusion and full completed-text coverage: a 96-chapter retrieval
scan measured about 0.54 seconds, while serial reviews dominated response time. Coalesce an excerpt
only when another exact excerpt contains it at the same recorded chapter offset, preserving all text
and remapping location hints. Fuse source selection with a concise conversational draft when the
entire pool fits; otherwise retain all-candidate, budgeted source selection. Conversation is reference
context, never evidence. Ambiguous references can return a claim-free clarification from a closed
enum. Tentative readings must remain grounded; their factual clauses get the normal checks.

Freeze each claim's own citations before overlapping positive span coverage with an independent
semantic/completeness review. Every original claim unit and exact source reference must validate;
neither review can borrow another claim's proof. Join and account for all started work before failure
or publication, admit at most two reviews, and stop scheduling after a rejection. A malformed audit
fails closed; a valid substantive rejection permits the existing one repair. No draft is displayed.
Selected-passage review now also checks the actual question and bounded conversation, allowing
linguistic explanation without importing book facts.

The existing reviewed-answer bank and off-request preparation remain. Its scope now includes the
pipeline/review policy and a new version, as well as the original book incarnation, reset epoch,
source/memory snapshot and model identity. Private contextual turns and selections never enter it.
Do not speculate on a typed message or persist personal conversation to gain cache hits.

Native OpenAI API, same Terra model, two runs of six cases through eight completed Karamazov chapters,
cache disabled; one run alternated baseline/candidate order. Timings include the complete checked
response, clarification/refusal, and the baseline's one 502. This small sample is not a population p95.

| Measure | Terra baseline | Conversation + Terra Fast |
|---|---:|---:|
| Requests | 12 | 12 |
| Median response | 15.206 s | 4.724 s |
| Slowest in those runs | 48.216 s, withheld | 7.964 s |
| HTTP 200 | 11/12 | 12/12 |

An additional running-service cold request needed repair and took **17.180 s**; its next exact checked
cache hits took **17.3 and 14.8 ms**, with zero model calls. Three selected-passage replies took
2.15–3.31 seconds versus baseline 2.53–3.00 seconds; this did not establish a selection speed win.
The benefit there is question-aware verification. Fast's effect is not isolated from generative
variance. Standard processing remains available. Earlier merged-review and higher-effort prototypes
were not retained: the former saved little while combining two independent checks, and the latter
produced more false withholding and much longer replies in the exploratory run.

In the direct proof comparison, both paths rejected all fourteen unsupported assertions and accepted
six supported controls (including a tentative reading after correcting its fixture question). A
separate swapped-citation case was repaired by the original audit; immutable own citations caused the
new path to withhold it instead. Scope included roles, cause, kinship, quantities, speaker attribution,
negation, lost uncertainty, fabricated history and future outcomes. These synthetic checks and one
novel are limited evidence, not a substitute for a blinded reader study.

Final verification: **1710 backend passed / 83 skipped**, **205 frontend passed**, clean Ruff,
TypeScript/Vite build and diff checks. Real Chrome rendered the EPUB and a cited reply; pending UI
showed only the reader's question and checking state. The in-app browser did not render the EPUB's
blob frame, although its conversation worked; use Chrome for this local preview. Existing source data
was preserved; the service uses a SQLite-backup copy. No merge or push.
Existing dependencies were reused to avoid a duplicate installation: Python 3.13, OpenAI SDK 2.45.0,
Vite 6.4.3 and Vitest 3.2.7. The frontend suite passed in that environment; the newer locked Vitest
4.1.11 environment was not installed or separately verified.

Re-run paired requests with `backend/scripts/benchmark_conversation.py` and an explicit copied data
directory, book ID and JSON case plan (or the retained validation record). It records real provider
spend but never changes reading positions. Follow-up evidence should broaden books, languages and
real reader reactions before changing defaults or claiming instant cold replies.

Research: [OpenAI latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization),
[prompt cache boundaries](https://developers.openai.com/api/docs/guides/prompt-caching),
[Fast processing](https://developers.openai.com/api/docs/guides/fast-mode),
[reciprocal rank fusion](https://cormack.uwaterloo.ca/cormack/cormacksigir09-rrf.pdf), and
[Self-RAG](https://arxiv.org/abs/2310.11511). Retaining RRF and independent review, rather than adding
another query rewrite/critic call, is an engineering choice from the measurements above; those
sources do not establish this application's quality or latency.

## Current local run: page context and ChatGPT subscription

The running preview now uses ChatGPT-authenticated Codex CLI 0.167.0 with Terra. Its launcher loads
no production environment file or API keys. API credentials are stripped from CLI subprocesses;
authentication must report ChatGPT and there is no API fallback. Native API configuration remains
available for production. The historical API timings above do not measure this transport.

Page turns persist a unique exact EPUB body-text anchor for text before the visible page. Sending a
question additionally confirms text through that visible page, labeled “Through this page.” The
server resolves a Unicode source offset, verifies the source digest and import manifest/store, and
clips the chapter before retrieval, names, drafting, review or caching. No chapter extraction or
embedding call is needed for this route. A bounded parse cache and exact recent scene windows keep
preparation deterministic; the existing bounded lexical search still covers earlier read text.

Drafting and source choice stay fused when the candidate pool fits, followed by two independent
overlapping reviews. Foreground subscription calls have two slots; background extraction has a
separate single-call lane. Speculative model prewarming is disabled locally. Only checked standalone
answers enter the reusable cache, keyed by exact source prefix, incarnation, reset epoch, model and
review policy. Private conversation and selections are not shared. Old in-flight snapshots stay
narrow during a forward page turn. Resets remove their durable boundary without deleting memory.

A synthetic EPUB was read and discussed in real Chrome with completed bookmark and ingest progress
both **zero**. The first answer located a key; the follow-up treated distrust as tentative and offered
independence as an alternative, with exact source citations. That follow-up took **11.825 s** over
the wire. Additional full HTTP requests: mid-chapter recap **13.552 s**, disagreement **10.748 s**,
unread-outcome refusal **3.533 s**, exact checked repeat **12.6 ms / zero model calls**. Both substantive
requests used three calls (draft plus parallel evidence and spoiler/completeness checks); refusal
used one. The unread ending and next-chapter canaries were absent from all outputs. Unit tests also
assert their absence from every model prompt. This is a small synthetic acceptance sample, not an
instant-cold-reply claim or a controlled speed comparison against the earlier API transport.

Existing source data is untouched; the preview still uses its copied library. Existing vector spaces
are not rebuilt or repinned. New local test books use offline lexical embeddings for chapter memory;
older pinned stores may need their original embedder for further ingestion, while prefix chat works
independently. Hosted page-context synchronization is not implemented; the flag defaults off.

Final checks: **1719 backend passed / 83 skipped**, **210 frontend passed**, clean Ruff,
TypeScript/Vite build and diff checks. After restart the durable page boundary and checked cache
survived; the cached reply returned in **13.2 ms**, explicitly attributed to the ChatGPT subscription.
The preview is left running locally on this branch, with no merge or push.

Authentication reference: [Codex authentication](https://learn.chatgpt.com/docs/auth).

## Current local conversation — 9 November 2026, field follow-up

The original v7 comparison used eight read chapters and was not representative of the user's
96-chapter session. Replaying the actual conversation exposed four failures in eight ordinary
turns: every message entered evidence retrieval; identity retrieval overfavoured recent scenes;
clarifications were model-generated; and the fallback refused more than eight proof units before
making its verification call. The forced two-sentence draft default encouraged unnecessary clauses.
The subscription CLI also carries substantial harness context: even the arithmetic reply below
used 7,981 ChatGPT input tokens. These counts are not production API cost estimates.

The current local candidate uses typed Jev questions over separate latest-message and dialogue
fields. Explicit ambiguous-reference questions can ask a factual-claim-free clarification directly.
Greetings, thanks, emotional reactions needing no story assertions, and unrelated messages get a
brief Terra response plus an independent concrete-story-information check. A contaminated response
gets at most one independently checked repair. Jev does not write prose or arbitrate literary meaning.

Brief current-scene catch-ups use exact windows ending at the confirmed read offset, without global
search or source reranking. They ignore prior dialogue and may retain only a useful verified PREFIX
of complete claims: no edited words, reordered claims, removed qualifications inside a claim or
unchecked text. Every published claim still passes deterministic boundaries, its own cited-body
support, conditionality, location and causal checks. Detailed/multipart questions cannot use this
pruning policy. A scene recap can be reused across dialogue only at the identical source/reset/read
point and question; source identity is revalidated before reuse. Other contextual replies stay private.

Named-identity requests seed introductory primary-text windows instead of recent-action windows.
Other book questions retain the full bounded retrieval route. Terra drafts with low effort locally,
using one sentence when sufficient. When Jev cannot settle a claim, Terra audits that claim's exact
source spans while still checking the WHOLE answer for semantics and completeness. Previously
Jev-cleared claims keep their original citations; no fabricated span audits stand in for Jev decisions.
Dense requests remain bounded and can still be withheld. No accuracy thresholds were lowered.

Pure immutable lexical/visibility caches avoid rescanning the same exact permitted body. They contain
word sets, not generated story facts or cached authorization. Source hashes, reading epochs and exact
prefix checks still run on every request. Selected-passage assistance is unchanged. Per-phase timing,
tokens and sanitized failure traces are logged locally without source text or dialogue. Cache hits
report their own current timings. The UI distinguishes verification and reading-boundary failures.

### Measured field replay

Same fixed user turns, isolated full-book copies, caches/prewarm off. The v7 baseline was collected
in an alternating-order run; the final candidate was run afterward against that same baseline to
avoid repeated subscription spend. This is a small pilot with provider variability, not a p95 claim.

| Eight turns each | v7 baseline | Current candidate |
|---|---:|---:|
| Median complete HTTP response, including failures | 7.353 s | 4.402 s |
| HTTP failures | 4 | 0 |
| Unhelpful empty evidence refusals | 1 | 0 |
| ChatGPT input tokens | 100,756 | 61,044 |
| ChatGPT output tokens | 1,165 | 389 |

The fresh catch-up remained **7.375 s**, including **5.820 s** drafting; explicit ambiguous follow-up
was **0.201 s** with no Terra call. The arithmetic reply took **4.115 s**, including **3.732 s** drafting.
This fixes routing/reliability and avoids unnecessary verification, but does not solve instant fresh
generation through the subscription CLI. A separate eight-case factual/instruction/spoiler run returned
8/8 HTTP 200; its median was 6.359 s versus the retained original baseline's 10.294 s, with 95,945 versus
241,380 ChatGPT input tokens. More complex interpretations still used two Terra calls and took 10–11 s.

Source/safety contracts passed the backend suite (**1773 passed / 83 skipped**) and subsequent affected
checks; frontend **211 passed**, production build successful, Ruff/diff checks clean. Live uncited-dialogue
guards blocked all seven injected story-detail assertions; one harmless named clarification was
conservatively withheld in a fixed-response probe. Compiled clarifications avoid that failure mode;
actual generated dialogue can make one checked repair. Earlier v7 controls rejected 9/9 unsafe drafts
in both pipelines and accepted 7/7 supported controls versus baseline 5/7. These samples do not establish
a universal error rate. The single existing verification JSON retains current measurements and limitations.

Use this candidate only in the local preview. Production defaults and keys are unchanged; Terra uses
the ChatGPT subscription and Jev its encrypted existing credential. The installed app-server alternative
lacked the CLI configuration-isolation flags used here, so it was not adopted. Leave the branch unmerged.
