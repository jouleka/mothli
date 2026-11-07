# Mothli implementation review — 7 November 2026

User reviewed the local implementation and approved merging on 7 November 2026. Implementation branch: `feat/companion-simple-shell`.

The reader now has Contents, Notebook, Mothli, and compact reading settings. The old Recap, People & sections, and Reflect destinations and their unused UI/test tree were removed. Existing book positions, marks, preparation, and backend correction data are preserved.

Mothli uses real requests, a bounded recent conversation, contextual passage questions, recap/person shortcuts, expandable sources, and explicit saves to Notebook. Model names, usage receipts, and technical status cards are absent from the reading flow. Short labels remain alongside unfamiliar icons. The bookmark mascot respects the quiet page without looping animation.

Conversation and saved Mothli replies are local browser data scoped to owner, book, and reading pass. They are cleared with the existing sign-out purge. They are not server-synced. Existing marks retain their previous hosted/offline behavior. The marks export contains marks, not saved Mothli replies. Clearing a chat does not delete explicitly saved replies.

Review repaired contextual-query length limits, prevented private contextual requests entering reusable answer caches, restored source-return paths, protected phone chat from inert background handling, and exempted desktop chat controls from reader arrow keys. Retained tests cover those meaningful boundaries and existing reading behavior; obsolete destination tests were removed.

Local validation: TypeScript/Vite build; 204 remaining frontend tests; 300 focused backend question, evidence, selection, cache, and route tests; full backend Ruff checks. Desktop and simulated phone browser checks covered opening/closing Mothli, a real Napoleon recap, saving it to Notebook, existing note navigation and return, and draft preservation after failed follow-up. This is not physical-phone keyboard validation or a deployed/hosted acceptance run.

The live follow-up revealed and fixed an internal 500-character validator mismatch. A later request exceeded the per-call input estimate, although the server mislabeled it as a book cost ceiling. Source-selection batching now retains every candidate within the existing cap. No account spend or book-budget exhaustion was established. The original Carlo follow-up returned HTTP 200 with checked citations after the repair (35.8 seconds on the local Codex transport). No cap was raised.
