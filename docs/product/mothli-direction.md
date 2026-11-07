# Mothli

Selected by the user on 7 November 2026. Mothli names both the app and its AI reading companion.

An original little book moth represents the product: wings resemble the two leaves of an open book, with warm paper, lilac, and a small friendly face. Use the shared `MothliMark` component and `mothli-icon.svg` asset. Keep it quiet while reading; personality belongs in useful interactions, not compulsory animations or extra onboarding copy.

The first interaction identifies Mothli as AI. Keep provider/model diagnostics out of ordinary reading. The name is a selected product identity, not a statement that trademark or domain clearance has been completed.

## Positioning to test

**Mothli remembers your side of the story.**

The baseline is already competitive territory. BookPal markets progress-bounded recaps and chat; Readwise offers contextual document conversation and saved replies; ReadAware describes durable reading memory. These are vendor descriptions, not independent quality evaluations:

- https://www.getbookpal.com/
- https://docs.readwise.io/reader/guides/ghostreader/chat
- https://readaware.app/

A moth mascot, warm tone, personal memory, and spoiler claims individually are not established differentiators. Our hypothesis is that readers will choose a companion that helps them enjoy forming and changing their own interpretations, with very little interruption. This needs direct comparison and reader evidence.

## One experience to prove first

1. Select a passage and react in ordinary words. Mothli uses the attached text or asks one useful clarification. Acknowledging a reaction does not mean agreeing with a factual allegation.
2. Discuss a theory using only permitted pages. Mothli distinguishes source facts, interpretations, and the reader's own guess. It never confirms or dismisses a prediction using unread information.
3. Explicitly keep the theory or thought in Notebook, with its source and reading position. Personal ideas remain editable and separate from generated book facts.
4. Return after a break. An optional short catch-up reconnects the reader with both the scene and their own saved question. It must quote or summarize what they actually saved, never infer their emotional state.
5. When the reader asks, revisit the old theory against newly read evidence. Avoid unsolicited “you were right” signals or revelation notifications, which can spoil the experience.

These are proposed next steps. Current saved conversations and replies are browser-local; durable theory objects, personal catch-ups, and automatic reflection are not implemented by the branding change.

## Small validation plan

Start with five adult fiction readers who already read regularly. Let them use their own DRM-free books and observe the full reading/discussion/return loop over a week. Compare the same tasks against their usual reader and a close AI competitor where accessible. Record what they use without prompting and why they return or leave. This is exploratory research, not a statistical proof of demand.

Evaluate whether discussion helps them keep reading, whether a later return restores context, and whether responses feel conversational without agreeing blindly. Measure time to the first useful response and interruption of reading. The previous local follow-up took about 36 seconds; improving that matters before adding more destinations. Test spoiler boundaries, ambiguity, contradictory theories, and offline access separately from perceived friendliness.

Build the smallest successful loop first. Keep the existing Contents / Notebook / Mothli structure. A theory is saved inside the conversation and Notebook, not introduced as another permanent dashboard.

## Compatibility during the rename

Existing browser-storage keys, cookies, owner cache namespaces, archive format identifiers, database paths, and old CLI aliases retain their protocol identity. Changing a public name must not discard books, reading progress, annotations, conversations, saved replies, or backups. Public package names and preferred CLI commands use Mothli. The local checkout directory can keep its old path to preserve running tools; the GitHub repository and clone instructions use `mothli`.
