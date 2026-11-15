# Mothli companion: open items

Every step updates this file: tick what it closed, add what it found, and move nothing back into chat logs.

Taken from sections 6m to 6aa of `companion-audit-2026-11-14.md`; the section in brackets says where each item is described.

## Now: the voice

- [x] Collect the voice samples: done on 15 November once TypeSafe had credits again. 19 of 19 in `voice-samples.md`, one run, $0.024 of Jev (146 checks), stopped-at cap $0.08 not reached.
- [x] The owner reads `voice-samples.md` and marks the replies that sound wrong. Done on 15 November as a direction, not marks: a friend who reacts, teaches and plays, never question in and answer out. The voice instruction was rewritten to it (6y) and the same 19 messages collected again in `voice-samples-after.md`. The owner: "not perfect, but not bad either"; go with it, tweak later.
- [x] Voice, still short of the direction: no real jokes, comparisons or sayings in 19 replies, and "catch me up" got longer (six long sentences) and still reads as a report. (6y) Closed for now in 6ac: 6 of 19 replies carry a quip of their own ("The bar is practically underground."), catch-up is four sentences. One run; the owner has not read this set yet.
- [ ] The fixed foreshadowing check cuts any sentence with the word "will", the noun included: "you surrender your will to him" was cut in five true sentences over two runs of the samples. Checked directly against the pattern, no model involved. A narrower pattern is a change to a spoiler check, so it waits for the owner's word. (6ac)
- [ ] A quip glued to a fact gets the whole sentence cut ("…a pretty startling bargain"): the writer is now told to give it a sentence of its own, where Jev doubts it and the second look passes it. Every quip therefore costs a second look. (6ac)
- [ ] When the checks drop a sentence, a later one that leans on it reads broken ("You're also in…" with nothing before it). Seen twice in 19 replies. (6y)
- [ ] In the home chat nearly every sentence about where the reader is trips Jev's time check and goes to a second look: 20 doubted sentences in 6 replies, none blocked. Slow, and a paid writer would pay for each look. (6y)
- [ ] Asked who kills Fyodor, Mothli refuses but volunteers that the narrator hints at a "catastrophe" from the inheritance dispute. The word is in the read pages, so it is inside the rule; the owner may not want it offered. (6y)
- [ ] Mothli knows the app from one hand-written paragraph (`APP_HELP` in `reader_place.py`); for anything else it is told to say it does not know. (6p)
- [ ] The checker drops correct sentences: in one run all seven sentences it removed were right, three of them from one recap. (6m, 6s)
- [ ] The checker passes a wrong time order: "after Adelaïda ran away and died, Fyodor forgot Mitya" gets through Jev and the second look. (6m)
- [ ] The length instruction is not reliable: a recap can still run to 12 sentences. (6m)
- [x] Mothli is not told about skimmed pages, so it says "I wasn't with you" for chapters the reader went through quickly. (6r) Closed in 6aa: the place line names the chapters before the reader's place that were gone through quickly here, by number only, apart from chapters leapt past; the speaking rules have Mothli say the two of them went through those quickly and ask, once, whether the reader read them; the "I've read…" button appears under that wording. Stand-in models only.
- [ ] How Mothli actually says it has not been seen: 6aa was not run with real models. To look at: whether it asks warmly and once, whether it still says "I wasn't with you" for skimmed chapters, whether Jev lets the new sentences through (place sentences already trip its time check, 6y), and whether real wording gets the button. (6aa)
- [ ] The reader's "I was just getting here" never reaches the server: the reader app keeps it in the browser (`rc:skipped-declined:{book}`). Mothli is told exactly what it was told before the answer, so it may still ask once in the book's conversation and once in the home one. Telling it needs the answer stored on the server and sent by the reader app. (6aa)
- [ ] "Already brought up" is read from Mothli's own stored sentences, in one conversation: the book's chat and the home chat each ask once, "Clear" starts over, and it looks at the last 200 stored turns. A sentence the patterns below do not know is not counted, and one that only states it without asking is. (6aa)
- [ ] The button's patterns for the new wording are guesses at English: the verb must come before the chapters ("Chapters 2 and 3 went by in a blur" gets no button), "turned the pages of chapters 2 and 3 so fast" gets none, and "I think she raced through chapter 3" gets one if chapter 3 was gone past. A missing button costs nothing that Contents cannot do; a stray one is still the reader's own "I've read…". (6aa)
- [ ] A chapter mostly read, with one page turned quickly, is named as gone through quickly, as in the reader app's own question. Mothli is also told "part of chapter N" was read with it. (6aa)
- [ ] The offer button ("I've read chapters…") never appears under a French reply: "I wasn't there" and the chapter numbers are matched in English only. (6t) The patterns added in 6aa are English only too.
- [x] The offer button has not been tried with real replies since its fix in 6t; unit tests only. (6t) Seen once with real replies in `voice-samples.md`: samples 10 and 11 carry "I've read chapters 1 to 4" and "I've read chapter 1". One run, English only.

## Before going live

- [x] Explain why chapter notes cost twice as much in the last run: $0.039 to $0.079 of Jev for the same 34 chapters and about the same sentences kept. (6t) Closed in 6x: the eval wrote them twice, itself and through the app's background writer, which marking chapters read starts. Counted with stand-in models: two writer calls and twice the checks for every chapter, in the script only. The app itself writes and checks a chapter once, however it hears that the chapter is finished. The eval now leaves the notes to the app's writer. The real run was not repeated.
- [x] The evaluation and the voice samples wrote and checked every chapter's notes again on every run, and the evaluation had no cap on Jev spend. Closed in 6x: notes are kept between runs in `backend/.eval-cache` and used while the chapter's text, the notes version and both models are the same (`--fresh-notes` writes them again); `companion_eval.py --max-usd`, default $0.10, stops the run and the app's notes writer; both scripts print the Jev total and how many chapters came from the cache. Stand-in models only.
- [ ] After a sentence is dropped, every later sentence of that reply, or of that chapter's notes, is checked by Jev a second time, and a doubted one gets its second look a second time. It is how the gate is written (later sentences are judged against what was actually shown), and it is the one place a sentence costs more than one check and one second look. A reply that arrives all at once pays it for every sentence after the drop, and `codex-local` does not stream. How much of real spend this is has not been measured. (6x)
- [ ] 6x was not run with real models: no dollar figure in it is measured, and the saving from the notes cache is arithmetic on 6t's figures (a repeat Pride and Prejudice set about $0.015 of Jev instead of $0.094). (6x)
- [ ] `companion_eval.py --max-usd` counts Jev only. The writer is the paid API unless `--transport codex-local` is given, and `--audit` calls it outside any ledger. (6x)
- [ ] The notes cache is keyed by the notes version, not by the wording of the notes prompt or of the checks: after changing either without a new version, run with `--fresh-notes`. (6x)
- [ ] `backend/scripts/benchmark_conversation.py` has no spend cap of its own; every other script that calls Jev has one, listed in 6x. (6x)
- [x] When Jev cannot be reached, each failed check stays in the book's cost ledger at its full reserved size; about 120 of them use up the book's token ceiling, and its notes are then refused (replies pass through the same ceiling). Seen in the voice-samples step, when TypeSafe had no credits. Closed in 6u for a call TypeSafe refuses outright (any 4xx, "no credits" among them): nothing is charged. A timeout or a server error is still charged at its reserved size, on purpose. Stand-in provider only.
- [ ] Check 6u against the real provider once it has credits: the shape of TypeSafe's error answer was guessed, so the reason word comes from the status alone and its own message may not be picked up for the log. (6u)
- [ ] Books already locked by the 15 November outage stay locked: their refused checks are in the ledger as `…-failed-reserved`, which cannot be told apart from timeouts afterwards. Nothing removes them. (6u)
- [ ] A long run of timeouts or server errors can still use up a book's ceiling, about 120 failed checks as before. Unknown spend stays counted by design; there is no limit on how much of a ceiling failures may take. (6u)
- [ ] While TypeSafe refuses, each reply and each notes run still pays the writer first and is then thrown away. Notes wait five minutes after "no credits" or a refused key; replies do not wait at all. (6u)
- [ ] The older, non-streamed reply path does not log the reason, and neither does anything else that calls Jev (extraction screen, cast, recap review); their refused calls are no longer charged. (6u)
- [ ] Accounts and server-side storage: chapter notes, the read map, the stored conversations and the home chat exist in local mode only; hosted mode has no tables or endpoints for them. (6m)
- [x] The chat panel inside a book still shows the browser's copy of the conversation; loading it from the server is what a second device needs. (6m) Closed in 6z: the panel shows `GET /api/books/{id}/conversation` and keeps only the unsent draft in the browser. Local mode only; hosted mode has no stored conversation to load.
- [ ] Phones: checked only at phone width in a desktop browser, never on a phone. (6p, 6s)
- [ ] Screen readers: neither chat panel has been tried with one. (6p, 6s)
- [ ] Cast entries that are not names: "his wife", "her divinity student". (6s)
- [ ] A skimmed page inside the current chapter blocks that chapter's notes until the reader presses "I've read this". (6n, 6r)
- [ ] A reader who starts mid-book gets no cast or recap until the earlier chapters are read or marked read. (6n)
- [ ] Newly skipped chapters are not asked about once any earlier chapter was declined. (6p, 6r) This is the reader app's question, from the browser's record; Mothli in conversation does not use that record and still raises them. (6aa)
- [ ] On the Project Gutenberg front-matter page of Pride and Prejudice, `PUT /api/books/{id}/read-page` answers 422, so that page can never be reported as read and the browser console shows an error. Seen in the owner's running app on 15 November; not examined.
- [ ] The shelf says "In chapter 3 of 61" for a book that opens on that front-matter page under the heading "Chapter I". Seen in the owner's running app on 15 November; not examined.
- [ ] The evaluation's reference is not given "already brought up" in the place line; no eval case skims pages, so nothing in it differs today. (6aa)
- [ ] The home chat uses the read text of at most six books, in shelf order, not the six that fit the question. (6o)
- [x] Earlier home turns are not filtered by what is read now: after "read it fresh" an old turn about that book still reaches the writer as conversation. (6o) Closed in 6v: a home turn is stored with the read map of every book that had anything read, and is said or shown again only while all of that is still read. Stand-in models only.
- [x] The conversation inside a book has the fault 6v closed for the home chat, in two shapes, both reproduced with stand-in models. Closed in 6w: a turn inside a book is stored with the book's read map and is said or shown again only while all of that is still read; the place rule is gone. Stand-in models only. As found: (a) A turn written while chapters were only marked as read, before any page was reported, is stored at place (0, 0), which always counts as read: after "read it fresh" it is still returned by `GET /api/books/{id}/conversation`, still listed as a reaction in `GET /api/reader/profile`, and is given to the writer again as soon as anything else is read. (b) A turn asked from a place that is still read, about a chapter the reader then marks "I haven't read this", stays shown and stays in the writer's and the checker's input. `visible_turns` looks at where the reader was, not at what the turn drew on. Storing the book's map with each turn, as home turns now do, would close both; turns already stored would then need the same decision as in 6v. (6v)
- [ ] One reset hides more of the home chat than it needs to: every reply is written with the text of every book that has anything read, so a turn counts as drawing on all of them, a greeting or a question about the app included. "Read it fresh" or "I haven't read this" on one book hides every home turn written since that text was first read, until it is read again. Telling a turn that holds nothing of a book from one that does would mean trusting a judgment about each sentence and about the reader's own message; no check gives that today. (6v)
- [ ] Home turns stored before 6v are never said or shown again, and neither are turns about a book later removed from the shelf. Their rows stay in the catalog until the reader clears the home conversation. (6v)
- [ ] Each home turn carries the whole read map of every read book, and every stored home turn is loaded and compared on each visit to the home screen and each home question. Not measured on a long conversation or a large shelf. (6v)
- [ ] A home screen already open keeps showing the turns it loaded: it asks the server again only when the shelf is opened anew. A reset made in another tab does not clear them from that screen. (6v)
- [ ] 6v has not been tried in the running app or with real models; stand-in models only. (6v)
- [x] The chat panel inside a book never asks the server for its conversation (the reader app only clears it), so a turn the server hides after "read it fresh" or "I haven't read this" stays on that panel from the browser's copy. The writer and the checker no longer get it. Needs the frontend change listed above from 6m. (6w) Closed in 6z: the panel loads the server's conversation on open, on a new reading pass, after a chapter is marked read or not read, after an "I've read…" offer and after "Clear"; "I haven't read this" empties it at once; an unreachable server shows no earlier turn. Frontend tests with the API mocked, and one browser run with stand-in models.
- [ ] The older, non-streamed reply path takes its earlier conversation from the browser's request, not from stored turns, so nothing filters it by what is read now. (6w) Since 6z the panel sends it only what is on the panel in this sitting, and reaches it only where streamed replies are off (the setting's own default, and hosted mode). Follow-up questions about a selected passage send their earlier exchanges the same way. (6z)
- [ ] Saved replies are a second browser copy: "Save" files a reply in the Notebook in the browser's storage, and nothing hides it when its chapter is marked not read. (6z)
- [ ] A reply the reader cancelled, or one the panel refused when it arrived, is still stored by the server, goes to the writer as conversation, and now shows at the next load. Stopping the request on Cancel would keep it from being stored. (6z)
- [ ] Replies the server does not store last only while the book is open: about a selected passage, a chapter closeout, a welcome-back recap, and every reply where streamed replies are off. They were in the browser's copy before 6z. (6z)
- [ ] A chat panel already open hears of a change made in another tab or on another device only when it is opened again or something read is changed in this tab. (6z)
- [ ] For five seconds after a reply arrives the panel keeps it even if a load of the server's conversation does not hold it, because the server stores a turn after its last sentence. Storing the turn before the last event is sent would remove the wait. (6z)
- [ ] An older turn on the panel has no "I've read…" button, and a question whose reply was wholly held back shows one fixed line, not the one the reader saw at the time: the server's copy carries neither. (6z)
- [ ] Marking a chapter not read redraws the chat panel twice in a few hundredths of a second. (6z)
- [ ] 6z has not been tried with real models, on a phone, with a screen reader or in hosted mode; the browser check was one run on one small book with stand-in models. (6z)
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
