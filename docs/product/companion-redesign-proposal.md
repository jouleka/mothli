# Reading companion redesign proposal

Draft for discussion, 7 November 2026. This proposes a revision to the companion experience in the [professional reader roadmap](professional-reader-roadmap.md); it does not replace accepted architecture or authorize implementation.

The product should help someone read, remember, and talk about a book with one familiar companion. The next release should reduce navigation and repair the meaning of reading progress before adding mascot interactions or more generated features.

## Findings from the current experience

The supplied screenshots and current implementation expose several different problems:

- Technical diagnostics dominate ordinary reading: provider names, model effort, embeddings, connection controls, and unpriced usage appear in settings, the reader, and conversations. These belong in developer diagnostics or a deliberate account setup flow.
- Four companion destinations contain overlapping tasks. Recap is both a destination and a question someone naturally asks. People and sections mixes locating a person, reviewing events, and navigating a book. Reflect suggests personal reflection but opens generated chapter takeaways.
- `AskTheBook.tsx` retains exchanges for display, but sends only the latest question and bookmark through `api.askBook`. `AskRequest` has no conversation history. Natural follow-ups such as “why did he do that?” lack the context the interface appears to promise. Its retained session also resets when the bookmark changes.
- Ask currently follows the general retrieval path even for a request like “what happened so far.” It should dispatch a recap request to the recap capability. Generic insufficient-evidence wording is not an adequate explanation for every failed task.
- The demonstrated book has reading progress at atom 8 while ingestion has reached atom 6. This establishes a preparation gap, not its root cause. The live status request also timed out during this audit. The exact cause of the stalled preparation remains to be diagnosed.
- `Companion.tsx` enables closeout only when ingestion reaches the current bookmark; `CompanionWorkspaceNavigation.tsx` disables Reflect otherwise. This explains a disabled destination in that state. It does not establish that every Reflect failure has the same cause.
- The demonstrated recap summarizes a list of maps; the chapter rail presents map and family-tree files as numbered sections. Acknowledgements can introduce names that are irrelevant to remembering the book. Internal EPUB atoms are being treated as meaningful reading units.

These are behavioral and content-model problems. Renaming the screens alone would preserve them.

## Research and design implications

[NN/G on recognition and recall](https://www.nngroup.com/articles/recognition-and-recall/) supports making choices and context available rather than asking users to remember commands. Proposed application: keep visible shortcuts for common companion requests even after combining them into conversation.

[NN/G on progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) recommends separating frequent tasks from advanced options. Proposed application: show the book, a companion entry, and saved material; move diagnostics, historical memory controls, and detailed relationship exploration out of the primary flow.

[Readwise Ghostreader](https://docs.readwise.io/reader/guides/ghostreader/overview) brings document and selection prompts into one chat and supports follow-up questions. This is evidence of an existing product pattern, not proof that our proposed design will outperform it. Our proposed distinction is continuity at the reader’s place, personal reading thoughts, and a recognizable companion.

[Google’s conversation design guidance](https://design.google/library/speaking-the-same-language-vui) emphasizes awareness of context and previous turns. Although written for voice, that principle applies to our conversational proposal: an interface that presents a discussion must support references to what was just said.

[Google’s source-chat documentation](https://support.google.com/gemininotebook/answer/16179559?hl=en) demonstrates citations that reveal a quote and navigate to its source. Proposed application: keep source access close to a response without making proof tables the main reading experience.

[Microsoft’s human–AI interaction research](https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/) covers expectations, normal use, errors, and change over time. Proposed application: give preparation delays, uncertainty, and service failures distinct explanations and recovery paths.

These sources inform the proposal. Navigation, tone, and preference hypotheses still need testing with readers; no usability study or competitor performance comparison has been completed.

## Companion identity and voice

The user selected **Mothli** on 7 November 2026 for both the app and its companion. Earlier drafts used a placeholder name. Domain ownership and trademark clearance remain separate launch tasks. One name and one small visual identity should work in the library, reader, and conversation.

Introduce it clearly as an AI reading companion at first interaction. Keep an accessible About and privacy explanation, including the actual service providers and permission for sending reading material. A friendly name should not imply we trained the underlying foundation model or that a human is responding. [EU AI Act Article 50](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R1689) addresses informing people about AI interaction, including clear disclosure by the first interaction; exact launch obligations depend on the distribution context.

Ordinary language should be warm, concise, observant, and occasionally playful. Avoid forced enthusiasm, constant emojis, baby talk, possessiveness, guilt, or pretending to have human experiences. Match the seriousness of the passage and reader. Cute behavior should be optional and respect reduced motion.

The following are response patterns, not factual statements about a particular book:

| Reader says | Proposed behavior |
| --- | --- |
| “Omg why would he do that?” | Use the current selection and recent conversation to identify the person and action. If ambiguous, ask one short question. Answer from the read material and distinguish interpretation from established facts. |
| “That was so unfair.” | Acknowledge the reaction without demanding a factual question. Invite discussion only when useful; do not invent or agree with an unsupported account of events. |
| “Wait, who is she again?” | Give a short reminder tied to the current scene and previously read passages. Offer a small person card. |
| “I forgot everything.” | Offer a brief recap immediately, with an option to expand it. Do not send the phrase through generic question retrieval. |
| “Is he going to die?” | Do not consult unread material. Offer to discuss what the reader has seen so far, without hinting whether the question is predictive. |
| “Remember this theory.” | Save it as the reader’s theory, visibly separate from book facts, with an easy way to edit or delete it. |

## Proposed navigation

The library remains the place to find a book and continue reading. Inside a book, there are three clear activities: **Read**, **Talk to Mothli**, and **Notebook**. These are activities, not three large dashboards or compulsory bottom tabs.

The reading toolbar contains Back to library, Contents, Notebook, Talk to Mothli, and a compact reading-settings menu. Appearance and listening controls live in that menu or their compact playback surface. Move Start over to book settings with its reset consequences explained; it is not an everyday reading action.

Desktop: the companion opens as a modest side panel that can be resized or closed. The page remains primary. Mobile: open a consistent conversation sheet with an obvious Close or Back to reading control and no composer/keyboard collision. Preserve the exact reading location, selection, and conversation scroll when switching. A source jump has a clear return-to-conversation action.

Use plain labels for navigation. Warm copy belongs in messages and small empty states, not in competing names for the same destination. There is one way to open the conversation; contextual shortcuts enter that same place with the relevant person, passage, or task attached.

## Changes to each existing section

| Current surface | Proposed decision | Result |
| --- | --- | --- |
| Library | Keep and simplify | Continue reading is the primary action. Add a book is visible. Remove the technical status card. Offer a return recap only when relevant and ready. |
| Reading sidebar | Replace | Remove the dashboard of counters, task tiles, and repeated status. Show the companion conversation only when opened. Use one small progress label tied to the actual book structure. |
| Ask the book | Rebuild as Talk to Mothli | A continuing per-book conversation with short replies, contextual actions, follow-ups, and optional source expansion. |
| Recap | Keep the capability; merge its entry into conversation | A Catch me up shortcut and natural requests produce the same recap. Default to a short continuous account. An expanded recap is an inline view with a clear return, not another permanent top-level world. |
| People and sections | Remove as a combined destination | Person reminders appear through conversation or selecting a name. Chapter navigation belongs in Contents. Chapter review uses meaningful chapter titles. Relationship detail is optional expansion from a person card. |
| Reflect | Remove the top-level destination | Personal thoughts are available at any time through conversation and Notebook. Useful generated chapter takeaways can later become an optional conversational action once dependable. |
| Notebook | Keep as the home for saved material | Show reader notes, highlights, bookmarks, and explicitly saved companion responses. Keep their origins clear. User thoughts never require model readiness. |
| AI status and usage | Relocate | Normal setup and billing live in Settings; model effort, embeddings, traces, and test configuration live in developer diagnostics. Show actionable service problems next to the affected task. |

Keep detailed people exploration as a later secondary capability if testing shows a need. Remove the current constellation, event-count rail, Roman-numeral memory slider, and duplicated counters from the primary experience. Preserve existing data and correction capabilities underneath the simpler presentation.

## Conversation behavior

The opening state identifies the book and offers up to three useful shortcuts: **Catch me up**, **Who was that?**, and **Talk about this passage** when a selection exists. Empty input says something simple such as “What’s on your mind?” The send action is an ordinary send button, not “Ask with citations.”

The server should resolve the reader’s intent into a supported operation: recap, person reminder, passage explanation, factual question, interpretation, reaction, or save a thought. Use existing retrieval, recap, and annotation capabilities rather than creating parallel engines for each new phrase. Ambiguous requests need one useful clarification, not a menu of technical modes.

Persistent conversation history is scoped to the owner, book, and reading pass. Retain bounded recent turns and a compact conversation summary for follow-ups. A stored conversation is not authoritative evidence about the book. Resolve claims against permitted source text on every turn. Switching books, clearing a conversation, resetting a reading pass, or changing owners must not reuse another scope’s context.

Personalization starts with explicit saved preferences and thoughts. Readers can see, correct, export, or delete them. Do not silently turn every emotional comment into a permanent personal profile. Cross-book personal memory is a later opt-in capability.

Facts need traceable source support. Interpretations should be expressed as interpretations; the system should not claim to know an author’s intent or a person’s hidden motive. Small source markers or a “From these pages” expansion keep verification available without making every response look like a report. Source labels must not reveal unread titles or events.

## Repair the book structure before generating better recaps

Keep separate representations for EPUB storage atoms, meaningful book units, and what source text the reader is permitted to discuss. Do not renumber or mutate existing atom identities merely to make the UI prettier.

Cover pages, map lists, acknowledgements, illustrations, notes, and indexes remain readable and locatable. They should not automatically supply the main recap, narrative character count, or meaningful chapter count. Image-only pages contribute no invented textual facts. Introductions and prefaces need content-aware handling: some contain substantive argument or narrative and cannot be excluded just because of their location.

For a biography such as the demonstrated book, prefer the actual chapter title over “Section IX.” A recap should help recover events and relationships in the author’s account; an argument-led nonfiction book needs claims and concepts rather than a forced cast of characters. Let readers correct an uncertain book classification without exposing implementation terms.

The current fully completed-chapter boundary remains the default for book-wide answers. A selected passage can be discussed immediately within its explicit scope. Broader support for the partially read current chapter requires a validated source-range design first; opening or jumping to a chapter is not proof that the reader has read all of it. All supplementary context, including future historical facts about a biography’s subject, must respect the product’s spoiler policy unless the user explicitly opts into a separately designed mode.

Preparation should not be the only route to useful help. Investigate a bounded retrieval path over permitted raw passages when structured memory is behind. It must preserve the same source and spoiler checks. Do not bypass safety gates to make the chat appear responsive.

## Truthful states and recovery

| Actual state | Proposed reader-facing behavior |
| --- | --- |
| Nothing substantive has been read | “We’re at the beginning. Pick a passage and we can talk about it.” |
| Companion preparation is behind | “I’m still catching up with your last chapter.” Show a concrete retry or scoped alternative. Never claim the book contains no answer merely because processing is behind. |
| No support found after a successful search | Explain the limitation briefly and invite a passage or a more specific reference. Do not infer that an answer must appear later. |
| Ambiguous person or pronoun | Ask which person or attach the relevant visible selection. |
| Provider or network failure | “I couldn’t get a reply just now. Try again?” Keep the draft and previous conversation intact. |
| A note is being saved offline | Save it locally, show its sync state, and keep it editable. |
| An answer fails verification | Say the answer could not be checked; do not present it as a missing fact in the book. |

Expose granular internal result reasons so these states are real, not cosmetic rewrites of one insufficient-evidence response. Reading, typing a note, and revisiting saved thoughts must continue while AI is unavailable.

## Visual direction

Carry the existing warm paper, purple accent, and book imagery into a calmer shared visual language. Remove oversized decorative headings, large empty zones, and separate visual “worlds” for overlapping tasks. Conversation uses comfortable UI text and readable line lengths; reserve expressive serif typography for book titles and brief editorial moments.

Give the companion one small identifiable presence. A restrained glance or bookmark motion can signal availability; it must not compete with the page. Sources, saved notes, person cards, and recap responses should look like related objects from the same app. The notebook’s personal material should remain visibly distinct from generated material.

## Implementation order and release gates

1. **Repair trust and book semantics.** Diagnose preparation stopping at the demonstrated read boundary, distinguish failure states, route recap requests correctly, classify meaningful units, and prevent paratext from dominating recaps and people lists. Confirm these behaviors on synthetic illustrated EPUBs, fiction, biography, and argument-led nonfiction. Preserve current reading progress and annotations.
2. **Review a small set of designs.** Create desktop and phone layouts for the reader, companion conversation, Notebook, and a returning-reader flow. Cover long responses, keyboard-open states, slow responses, empty states, citations, and offline notes. Test labels and navigation before implementation.
3. **Build the simpler shell and real conversation.** Remove redundant destinations, add bounded conversational context and durable per-book history, connect existing recap/person/selection operations, and move diagnostics to their proper settings surface. Ship this as one coherent vertical slice, not a succession of restyled old modals.
4. **Validate reading use.** Ask a small initial group, for example five readers, to return after a gap, request a recap, identify a person, react casually, save a thought, and return from a citation. Observe first-click success and confusion; this is formative feedback, not statistical proof. Record answer usefulness, failed-task recovery, time away from the book, and latency alongside qualitative feedback. Set performance targets after measuring real device and provider behavior.
5. **Add personality and extensions only after the core works.** The bookmark buddy becomes the companion entry; theory pockets become saved reader thoughts; the scrapbook grows from Notebook; return reminders use the existing recap and history. None should require another top-level navigation destination.

The first acceptance scenarios are concrete: “catch me up” yields a useful recap rather than generic refusal; “why did he do that?” uses the previous turn or asks a sensible clarification; a person reminder returns to the same page; a personal reaction does not require proof; an AI outage leaves notes usable; a citation cannot expose unread material; an illustrated page cannot block progress; a bookmark change does not erase the conversation.

## Decisions to settle before implementation

Agree on the one-conversation structure, the Notebook boundary, removal of the four-workspace navigation, and the working companion personality first. The final name and detailed mascot can follow the initial interaction designs and naming checks. Keep existing backend safety and data ownership guarantees while changing how the reader experiences them.
