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
The validated local run also enables native Terra `READER_FAST_MODE=true`; this has premium
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
