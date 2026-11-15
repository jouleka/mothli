# Mothli companion: open items

Every step updates this file: tick what it closed, add what it found, and move nothing back into chat logs.

Taken from sections 6m to 6w of `companion-audit-2026-11-14.md`; the section in brackets says where each item is described.

## Now: the voice

- [ ] Collect the voice samples: `backend/scripts/voice_samples.py` is written, but `voice-samples.md` is not, because TypeSafe refused every Jev check for lack of credits (15 November). Add credits, then run the script once.
- [ ] The owner reads `voice-samples.md` and marks the replies that sound wrong; the next step changes how Mothli talks and collects the same messages again with the same script.
- [ ] Mothli knows the app from one hand-written paragraph (`APP_HELP` in `reader_place.py`); for anything else it is told to say it does not know. (6p)
- [ ] The checker drops correct sentences: in one run all seven sentences it removed were right, three of them from one recap. (6m, 6s)
- [ ] The checker passes a wrong time order: "after Adelaïda ran away and died, Fyodor forgot Mitya" gets through Jev and the second look. (6m)
- [ ] The length instruction is not reliable: a recap can still run to 12 sentences. (6m)
- [ ] Mothli is not told about skimmed pages, so it says "I wasn't with you" for chapters the reader went through quickly. (6r)
- [ ] The offer button ("I've read chapters…") never appears under a French reply: "I wasn't there" and the chapter numbers are matched in English only. (6t)
- [ ] The offer button has not been tried with real replies since its fix in 6t; unit tests only. (6t)

## Before going live

- [ ] Explain why chapter notes cost twice as much in the last run: $0.039 to $0.079 of Jev for the same 34 chapters and about the same sentences kept. Likely, from reading the code and not confirmed by a run: the eval writes them twice, itself and through the app's background writer, which marking chapters read starts. (6t)
- [x] When Jev cannot be reached, each failed check stays in the book's cost ledger at its full reserved size; about 120 of them use up the book's token ceiling, and its notes are then refused (replies pass through the same ceiling). Seen in the voice-samples step, when TypeSafe had no credits. Closed in 6u for a call TypeSafe refuses outright (any 4xx, "no credits" among them): nothing is charged. A timeout or a server error is still charged at its reserved size, on purpose. Stand-in provider only.
- [ ] Check 6u against the real provider once it has credits: the shape of TypeSafe's error answer was guessed, so the reason word comes from the status alone and its own message may not be picked up for the log. (6u)
- [ ] Books already locked by the 15 November outage stay locked: their refused checks are in the ledger as `…-failed-reserved`, which cannot be told apart from timeouts afterwards. Nothing removes them. (6u)
- [ ] A long run of timeouts or server errors can still use up a book's ceiling, about 120 failed checks as before. Unknown spend stays counted by design; there is no limit on how much of a ceiling failures may take. (6u)
- [ ] While TypeSafe refuses, each reply and each notes run still pays the writer first and is then thrown away. Notes wait five minutes after "no credits" or a refused key; replies do not wait at all. (6u)
- [ ] The older, non-streamed reply path does not log the reason, and neither does anything else that calls Jev (extraction screen, cast, recap review); their refused calls are no longer charged. (6u)
- [ ] Accounts and server-side storage: chapter notes, the read map, the stored conversations and the home chat exist in local mode only; hosted mode has no tables or endpoints for them. (6m)
- [ ] The chat panel inside a book still shows the browser's copy of the conversation; loading it from the server is what a second device needs. (6m)
- [ ] Phones: checked only at phone width in a desktop browser, never on a phone. (6p, 6s)
- [ ] Screen readers: neither chat panel has been tried with one. (6p, 6s)
- [ ] Cast entries that are not names: "his wife", "her divinity student". (6s)
- [ ] A skimmed page inside the current chapter blocks that chapter's notes until the reader presses "I've read this". (6n, 6r)
- [ ] A reader who starts mid-book gets no cast or recap until the earlier chapters are read or marked read. (6n)
- [ ] Newly skipped chapters are not asked about once any earlier chapter was declined. (6p, 6r)
- [ ] The home chat uses the read text of at most six books, in shelf order, not the six that fit the question. (6o)
- [x] Earlier home turns are not filtered by what is read now: after "read it fresh" an old turn about that book still reaches the writer as conversation. (6o) Closed in 6v: a home turn is stored with the read map of every book that had anything read, and is said or shown again only while all of that is still read. Stand-in models only.
- [x] The conversation inside a book has the fault 6v closed for the home chat, in two shapes, both reproduced with stand-in models. Closed in 6w: a turn inside a book is stored with the book's read map and is said or shown again only while all of that is still read; the place rule is gone. Stand-in models only. As found: (a) A turn written while chapters were only marked as read, before any page was reported, is stored at place (0, 0), which always counts as read: after "read it fresh" it is still returned by `GET /api/books/{id}/conversation`, still listed as a reaction in `GET /api/reader/profile`, and is given to the writer again as soon as anything else is read. (b) A turn asked from a place that is still read, about a chapter the reader then marks "I haven't read this", stays shown and stays in the writer's and the checker's input. `visible_turns` looks at where the reader was, not at what the turn drew on. Storing the book's map with each turn, as home turns now do, would close both; turns already stored would then need the same decision as in 6v. (6v)
- [ ] One reset hides more of the home chat than it needs to: every reply is written with the text of every book that has anything read, so a turn counts as drawing on all of them, a greeting or a question about the app included. "Read it fresh" or "I haven't read this" on one book hides every home turn written since that text was first read, until it is read again. Telling a turn that holds nothing of a book from one that does would mean trusting a judgment about each sentence and about the reader's own message; no check gives that today. (6v)
- [ ] Home turns stored before 6v are never said or shown again, and neither are turns about a book later removed from the shelf. Their rows stay in the catalog until the reader clears the home conversation. (6v)
- [ ] Each home turn carries the whole read map of every read book, and every stored home turn is loaded and compared on each visit to the home screen and each home question. Not measured on a long conversation or a large shelf. (6v)
- [ ] A home screen already open keeps showing the turns it loaded: it asks the server again only when the shelf is opened anew. A reset made in another tab does not clear them from that screen. (6v)
- [ ] 6v has not been tried in the running app or with real models; stand-in models only. (6v)
- [ ] The chat panel inside a book never asks the server for its conversation (the reader app only clears it), so a turn the server hides after "read it fresh" or "I haven't read this" stays on that panel from the browser's copy. The writer and the checker no longer get it. Needs the frontend change listed above from 6m. (6w)
- [ ] The older, non-streamed reply path takes its earlier conversation from the browser's request, not from stored turns, so nothing filters it by what is read now. (6w)
- [ ] One "I haven't read this" hides more of a book's conversation than it needs to: a turn is stored with the whole read map, so every turn written while that chapter was read is hidden until it is read again, whatever the turn was about. (6w)
- [ ] Turns inside a book stored before 6w are never said or shown again. Their rows stay in the catalog until the reader clears that book's conversation. (6w)
- [ ] Each turn inside a book carries the book's whole read map, and every stored turn of a book is loaded and compared on each question, each load of its conversation and each load of the profile. Not measured on a long conversation or a long book. (6w)
- [ ] 6w has not been tried in the running app or with real models, and the evaluation was not run with its changed seeding; stand-in models only. (6w)
- [ ] Home chat spend is recorded against the first book on the shelf, because the cost ledger has no place for a conversation that belongs to no book. (6o)
- [ ] The event-binding check does nothing on streamed replies: an invented killing or marriage produces no hit. (6m)
- [ ] Reply times on the production path have not been measured; every timing so far is through `codex-local`. (6m)
- [ ] The jump and start-over dialogs have not been opened in a browser; unit tests only. (6n)
- [ ] Chapter closeout can appear one page late. (6n)
- [ ] The Gutenberg import fix was checked on three books; no others. (6s, 6t)

## Later

- [ ] No suggestions of books that are not on the shelf: that needs a description of each book known to give nothing away. (6o)
- [ ] A book not started has no description at all, so Mothli can say nothing about it. (6o)
- [ ] An import with no highlights and no "finished" status gives no offer to place the reader. (6n)
- [ ] The older, non-streamed reply path does not use the read map. (6n)
- [ ] Cast and recap can show something from a skimmed page of a chapter otherwise finished; accepted on purpose, to be looked at again. (6n)
- [ ] Verne's chapter XII stays inside chapter XI, because the book's own contents list has no entry for it. (6t)
- [ ] Eval: the unread-name and internal-term checks misread French ("connaître", "passage"). (6s, 6t)
- [ ] Eval: three patterns mark right replies wrong (`p-why-stated`, "Darcy thinks", Du Bois on Washington). (6s, 6t)
- [ ] Eval: the 11 shown sentences the reference rejected in 6s were not checked by hand. (6s)
- [ ] Eval: Pride and Prejudice facts fell from 23 to 20 of 25 on the read map; one run cannot tell the map from chance. (6t)
- [ ] Eval: no re-reading case has been run with real models, and most results are one run. (6n)
- [ ] Not measured: whether notes make the time-order slip more likely, and how often a shown sentence rests on a note alone. (6m)
