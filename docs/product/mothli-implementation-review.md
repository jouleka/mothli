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

## One subscription generation plus Jev experiment — 9 November 2026

Installed and applied the official [TypeSafe skill](https://github.com/typesafe-ai/skills/tree/main/skills/typesafe-ai)
using its npx method for Codex. The original local skill still owns encrypted Windows credential
access; the credential was restored without placing it in chat, the repository, or plaintext files.
ChatGPT generation continued through the subscription; no OpenAI API key was loaded.

`READER_CONVERSATION_JEV` is an explicit local page-chat experiment, default off. It retains exact
source-prefix clipping, immutable own citations, deterministic source/name/binding/prolepsis gates,
and cache scoping by model, policy, source and reset epoch. It permits one generation attempt only.
Jev batches narrow assertion-type/support, relevance, omitted-condition and unestablished-outcome
judgments. Code combines their probabilities; distribution concentration is not treated as accuracy.
Unsupported, uncertain, malformed and unavailable reviews never release a draft. There is no hidden
generative retry or fallback. Oversized retrieval pools use bounded Jev relevance ranking before
generation; small pools need just one post-draft Jev request. Selected-passage assistance retains its
existing independent checks and is outside this experiment.

Comparison used six synthetic mid-chapter cases and two first-eight-chapter Karamazov replays, no
answer caching or speculative preparation, and temporary imported books. Original library files and
positions were preserved. Baseline HTTP measurements were retained from the initial alternating-order
run to avoid repeating subscription spend while revising the typed packet. This is a small,
development-oriented experiment with generative variance, not a blinded quality study or a p95.

| Eight HTTP requests | Current pipeline | One generation + Jev, final policy |
|---|---:|---:|
| Median complete HTTP response, including refusals/errors | 15.741 s | 11.639 s |
| Longest HTTP response in this sample | 37.951 s | 13.128 s |
| HTTP 200 | 8/8 | 3/8 |
| Substantive questions answered with cited claims | 6/6 | 0/6 |
| ChatGPT input tokens | 289,005 | 72,521 |
| ChatGPT output tokens | 2,259 | 567 |

The 75% input-token reduction includes withheld answers; it is **not** a cost-per-useful-answer win.
Recorded Jev spend for these requests was $0.00177576; requests plus the final development controls
cost $0.00268156 in Jev usage. ChatGPT counts include CLI harness overhead. Illustrative uncached
Terra API equivalents were $0.300300 versus $0.077133 for the eight requests, not actual API bills
or production cost estimates. No subscription price is inferred from its token usage.

On 17 fixed reviewer controls, the baseline accepted all six supported cases and rejected ten of
eleven negatives; one negative had a provider/launcher timeout and is unavailable, not a rejection.
Jev rejected all eleven negatives and accepted five of six positives. A separate 14-case Jev holdout
rejected all eight negatives and accepted five of six positives, withholding a tentative reading.
Coverage included actors, quantities, negation, location/causal bindings, conditions, attribution,
kinship, forged conversation, prompt injection, swapped citations and unread outcomes. These small
sets establish no general error-rate guarantee. Three diagnostic retries after the main comparison
answered two factual/instruction questions (13.845 s and 10.317 s); the interpretive reaction remained
withheld (5.892 s). They are not substituted for the first-run results. Jev verification itself was
typically around 0.2 s; writing and the subscription transport remained substantial latency sources.

Decision: keep the experimental reviewer available but **disabled**. Its response availability does
not justify replacing the current pipeline. Do not lower safety thresholds merely to clear these
examples. The next direction should separate literal evidence screening from nuanced interpretation
and test an explicit uncertainty escalation policy rather than claim a universally successful
one-call answer path.

The experiment also exposed an npm Windows CLI shim timeout: killing the shim left Node/native
children holding stdout open. The adapter now resolves the installed official native executable,
so its subprocess timeout owns the inference process. One stalled benchmark child was verified and
stopped; its unavailable result remains recorded. The native transport passed a live subscription
probe and bounded/fail-closed contract tests. Final backend verification: **1737 passed / 83 skipped**,
clean Ruff and diff checks. No UI code changed. Leave the current pipeline running locally with this
timeout fix; no merge or push. The compact machine-readable record remains `conversation-validation.json`
in this task's outputs; the benchmark script reproduces cases without downloading dependencies/books.
