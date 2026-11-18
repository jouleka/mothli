# ADR 0044 — Story memory on demand: one book file, one set of notes

**Status:** Accepted (2026-11-18), with the owner's rule below. Amends ADR 0042 sections 2, 3, 4, 6
and 7 and its parts 3 and 5; amends ADR 0017 section 3 again (shared derived memory, now for every
book file, not only library editions built by the operator).

## The owner's rule on acceptance (18 November 2026)

His words: "users when they read they want the best possible experience be it imported books …
be it imported epubs be it free books … the notes are layers we are adding. users should have their
own quota for speaking with mothli, that's it. behind all that we take care. we can't tell users hey
you can't read this chapter, or you didn't pay for this, or this other user didn't finish that
chapter."

What that fixes, wherever the sections below say otherwise:

- **A reader has one visible allowance: talking with Mothli.** Notes are never sold, never tied to a
  plan and never counted against a reader. Option 3 of section 5, and the remark about a price for
  notes in question 1, are closed: there is no price for notes.
- **Mothli pays for notes, behind the scenes** (option 2 of section 5).
- **The guards of section 5 are protection against abuse only, and a reader who is really reading
  must never meet them.** They start at 30 new chapters a day and 300 a month that one reader's
  reading can be first to (not 12 and 120), and 5 new files a month stays. They are settings; raise
  them if a real reader ever reaches one. The notes ceiling for everyone starts at $10 a day and the
  platform day at $15, as recommended.
- **Nothing a reader is shown may say or imply** that they cannot read something, that they have not
  paid for something, that a limit of theirs was reached for notes, or anything about another reader.
  Reading is never held back by notes. Where notes are not ready the screens show what there is; the
  one existing line in Mothli's voice ("I'm a little behind you. I'll catch up on the rest tomorrow.")
  may stay, and no new line about limits is written. Every step that touches a screen checks this.
- Questions 2 to 5 are answered as recommended: notes go 30 days after the last holder of a file is
  gone; one chapter ahead; no reader corrects notes; the timing hint is accepted.

File and line references are to `main` at e4df206 (PR #120). Nothing in this record was run against a
database, a server or a model; every claim about what the code does comes from reading the lines
cited next to it. No code, test, migration or setting changes with this record.

## Context

### Words used here

- **Story memory**, which the owner calls **notes** and this record calls "the notes" from here on:
  what Mothli keeps about a book's story: the cast, a card per person, the ties between them, and the
  story so far, chapter by chapter (ADR 0042, Context). It is about the book, never about a reader.
  It is not the **chapter notes** feature (`chapter_notes`, `READER_STORY_NOTES`), which stays per
  reader and is not changed by this record.
- **The file**: the exact EPUB a reader added, byte for byte, whether they uploaded it or took it
  from the free library.
- **An edition**: one set of notes, belonging to one exact file. Two different files of the same
  novel (another publisher, another Project Gutenberg release) are two editions.
- **The door**: the one database function that hands a reader an edition's notes, only up to that
  reader's own place in the book (`library_story_memory`, migration
  `backend/app/hosted/schema/0026_library_story_memory.sql:66-252`).
- **The place**: the last chapter up to which the reader has finished every chapter, by the read map
  (`reading_state.memory_through`, `0026:7-8`), never more than the last chapter in which they have a
  read page (`0026:98-108`).

### How it works today

1. **Free-library books: built once, to the end, by the operator.** `python -m
   app.hosted.library_memory build 1342 11` (`backend/app/hosted/library_memory.py:4`) makes a
   reserved **library account** once (`:106-121`), adds each title to it by the very path a reader's
   add takes, which stores an encrypted copy of the file under that account (`:234-247`,
   `tenant/classics.py`), and queues a job that builds every chapter to the end
   (`companion/jobs.py:440-443`). Spend is the library account's own (ADR 0042 section 4).
2. **Each reader is handed only their part, by the door.** The door takes the book id and nothing
   else (`0026:66`); finds the calling reader from the transaction's owner setting (`0026:78`);
   accepts only a free-library add (`source_kind LIKE 'gutenberg:%'`, `0026:81`) whose `file_hash`
   equals a library-account book's (`0026:86-97`); works out the place from the reader's own rows
   (`0026:98-108`); and returns only rows stamped at or before it, hiding a later end (`invalid_at`)
   as no end (`0026:142-143` and alike). The web process reads every book's memory through it first
   (`backend/app/hosted/companion/memory.py:522-531`).
3. **A reader's own uploaded book: built per reader, switched off.** With
   `HOSTED_READER_STORY_MEMORY` on (`backend/app/config.py:155-158`, off by default), a finished
   chapter queues a job for the reader's own book (`companion/routes.py:79-86`), which builds memory
   under the reader's own `owner_id` up to their read map (`companion/jobs.py:445-450`). About
   $0.09 a chapter and about $7.30 for *Pride and Prejudice*, again for every reader (ADR 0042, "What
   was measured"). Off, an upload has no notes at all, and the screens say "I don't keep a list of
   who's who for this book yet" (`companion/routes.py:318-319`, ADR 0042 section 3 (A)).
4. ADR 0043 removed the reader's own AI key and the old whole-book `ingest_book` job, so the
   platform's account is the only payer of anything and the `story_memory` job is the only builder.

ADR 0042 section 3 (lines 276-279) rejected building library notes "when the first reader gets there",
because a cast appearing at once would tell a reader someone else got there first, and kept uploads
apart because "matching a private file to someone else's would reveal what a person owns" (line 283).
The owner has now decided both points the other way, knowingly; this record says below exactly what
that costs.

### What the owner decided (18 November 2026)

He asked why notes are built for chapters nobody has read, and why an imported book should differ
from a free one: "are notes custom for each user or smth? what does it matter if someone imports them
or not?", and "cant we build them for everyone as soon as someone reads them, for example the first
chapter, and when another reads the first chapter mothli already has the notes". Put to him as below,
he said "yes please makes much more sense":

**One book, one set of notes, whoever brings it.** Every book is treated the same. A book's notes are
written as the first reader reaches each chapter, and reused for every other reader who has the
identical file, free-library or imported. Nothing is built ahead for books or chapters nobody has
reached. Only Mothli's notes about the book's story are shared: never a reader's own highlights,
notes, conversations or reading position, and never the file. Each reader is still handed the notes
only up to their own place. Two different editions of one novel are different files and get their
own notes.

## Decision

### 1. Recognising the identical file

**What is fingerprinted, and when.** Today every hosted import, upload or free-library add, goes
through one function, `store_epub` (`backend/app/hosted/tenant/api.py:302-395`; "the upload route and
the free library's add both come here", `:314-315`). It computes **SHA-256 of the EPUB's bytes exactly
as received** (`:333`), before the file is stored; the object store is told to expect that hash
(`:358`) and encrypts the file under the reader's own object (`:311-312`); the hash is kept on the
reader's own `books` row as `file_hash` (`:374`, column `0002_library_memory_and_vectors.sql:10,25`).
Every later read of the file checks it again (`companion/source.py:83,107-112`). Because the
fingerprint is taken of the plain file before encryption, two readers' encrypted copies differ on
disk while their fingerprints are equal. Nothing new is hashed at upload for this.

**One addition: the fingerprint of how the file was split into chapters.** The notes are stamped by
chapter number. Two copies of one file imported on either side of a change to Mothli's chapter
splitter could number their chapters differently, and then "chapter 7" of the notes would be another
reader's chapter 8: a spoiler. Today that case fails closed on the story screens (the store
cross-check, `source.py:91-93`, test
`test_companion_story_memory_postgres.py::test_the_story_memory_screens_refuse_a_memory_built_from_another_split_of_the_book`),
but the chat path does not run that cross-check (ADR 0042 part 4 "Built on", item 5). So the import
also stores `books.split_hash`: SHA-256 over the ordered list of (chapter number, the file part of the
chapter key, SHA-256 of the chapter's text), computed from the chapters `store_epub` already has in
hand (`api.py:320,336-349`), ignoring the book id. **Identical means: same `file_hash` and same
`split_hash`.** Anything else is another edition. Nothing is deployed, so no book needs the new column
filled in afterwards.

**A free-library book and an imported copy of the same file** have the same fingerprint (the same
`store_epub`) and so the same edition. Which way a reader got the file no longer matters, and the
door's `source_kind LIKE 'gutenberg:%'` condition (`0026:81`) goes.

**What links a reader's book to the shared notes, and who can read it.** No new link row. The link is
the two fingerprints on the reader's own `books` row, compared inside the door each time (as today,
`0026:86-97`). That row is the reader's, under row-level security, as every row of theirs; it is
erased when they remove the book (the row is kept as a bare id with the hash cleared,
`tenant/repository.py:1816`) or their account. Readers' sessions cannot see any other reader's row or
any edition's row (section 4). The worker and the purge, which bypass row-level security by design,
can compare fingerprints across readers: that is the "Mothli internally knows two readers have the
same file" the owner was told about, and nothing a reader is sent ever states it.

### 2. Where the shared notes live, and who owns them

**The library account becomes the notes account.** One reserved user that never signs in (today's
library account, `library_memory.py:10-12`, `0026:10-17`), renamed `notes_account` and created by the
migration itself rather than by an operator command. Each edition is one `books` row of that account,
holding: the two fingerprints, the content language and book type (both read from the file, so equal
for every copy), a neutral title (`edition <first 12 hex of the fingerprint>`; never the title a reader
typed or a file name), no author, no source. The notes themselves are the ordinary memory rows
(`chapters`, receipts, `entities`, ... `0002:85-373`) under the notes account and that edition, exactly
as library rows are today. A unique index on (notes account, `file_hash`, `split_hash`) for live rows
makes one edition per file.

**Mothli keeps no copy of the file.** An edition row has no `source_objects` row (that table hangs from
`books`, `0002:30-48`, not the other way, so a book row without a file is allowed). Each chapter is
built from **the copy of the reader whose reading asked for it**, read through that reader's own
`HostedBookSource` and checked against its fingerprint (`source.py:83,107-112`), as a reader's own
build reads it today (`companion/story_memory.py:477`). The text lives only in the worker's memory for
that one chapter (an in-memory working copy, ADR 0042 part 2 "Built on" item 5) and is never published
(`story_memory.py:27`).

Why not keep a copy for the edition: an imported book is often someone's purchase of a copyrighted
work. A reader may keep their own copy in Mothli; a copy Mothli kept for itself would be Mothli holding,
and building from, a copyrighted file that no current reader holds, a library of other people's
uploads that outlives them. It would also break the promise that removing a book or deleting an account
erases it (ADR 0041 part 8 "Built on", `0041:867-868`). The free library's operator copy goes too:
those files are public domain, but keeping them is the only reason the library account stores files,
and building from the reader's own copy makes that unnecessary.

**What follows for the free library:** the operator no longer chooses titles or builds anything. A
free-library title gets notes the way any book does, chapter by chapter as readers reach them.

### 3. When a chapter's notes are written

**The trigger is the same as today's per-reader trigger.** A page that finishes a chapter, and "I've
read this" (`companion/routes.py:146-147,167-168`), ask for the book's notes job. A skimmed page
finishes nothing and asks for nothing (`routes.py:133-134`; skimmed pages are only remembered,
`read_map.py:116-151`). The setting that holds this back for uploads (`routes.py:85`) goes.

**The job stays the reader's**, one waiting and one running per reader and book
(`0025_story_memory_jobs.sql:15-18`), claimed, leased and cancelled as today (`hosted/jobs.py:25`,
`companion/jobs.py:183-217`), so erasing a reader erases their jobs and nothing about them is left in
the notes. What changes is where the run reads and writes: it builds the next chapter of **the
edition** that the reader's book matches, creating the edition row on the first chapter.

**How far it builds: one chapter ahead of the reader, decided.** Today a reader's job builds up to
`finished_through` (`companion/jobs.py:448-453`). Here the target is `finished_through + 1` (never
past the book's last chapter), and **0 until the reader has a non-skimmed read page anywhere in the
book**. So:

- chapter 1 is built when a reader first reads a page of the book, not when it is added: importing a
  file costs nothing (this matters for abuse, section 5);
- chapter *n*+1 is built when someone finishes chapter *n*. The door does not hand that reader chapter
  *n*+1's notes until they finish it (their place is still *n*), so the build has the whole of their
  reading of chapter *n*+1 to finish (two to three minutes on the subscription, ADR 0042 line 733;
  reading a chapter takes far longer). The furthest reader usually never waits; every reader behind
  them never waits.

What "one ahead" does to "nothing is built that nobody reached": **reached now means started, not
finished.** The notes for a chapter exist once somebody has finished the chapter before it. At most
one chapter per edition is built that may never be read (the chapter after the furthest reader's
place, if they stop there): about $0.09 once per file, not per reader. It also changes a protection
layer, said plainly in section 4: the notes of chapter *n*+1 exist while even the furthest reader is at
*n*, so for that chapter the guarded handover (the door) is the only thing between the notes and every
reader, never "the notes do not exist yet". For free-library books this is still less than today,
where the whole book exists before anyone reads it.

Building only on finishing (no one ahead) was considered. It keeps "nothing exists past the furthest
reader", but the furthest reader would wait two to three minutes after every chapter, and see "I'm a
little behind you" often, which itself tells them they are first (section 4). One ahead is
recommended; it is question 3 for the owner.

**Order.** Chapters are built strictly in order: chapter *n* is extracted against the cast of chapters
1 to *n*−1 and refused otherwise (`story_memory.py:483-484`, ADR 0042 fact 3). The next chapter to build
is the edition's first missing chapter, whoever's reading asks. A reader who jumps to chapter 10 by
turning pages has `finished_through` 0, so nothing past chapter 1 is built for them. A reader who marks
ten chapters "I've read this" raises `finished_through` to 10; their job builds chapters 1 to 11 one per
run, each within the guards of section 5 (about $0.99 if nobody built them before). Marking chapters
"not read" lowers the target; nothing already built is removed (others may need it), and the door
hands this reader less.

**Two readers asking for the same chapter at once: exactly one build.** Today
`begin_story_memory_chapter` writes the chapter a run works on into its job, under the lease
(`hosted/jobs.py:546-577`). It also writes the edition, and a new unique index on `jobs` over
(edition, chapter) for `story_memory` jobs in state `leased` or `running` lets only one run hold a
chapter, across all readers. The second run finds the chapter taken, spends nothing, and asks for its
next run a couple of minutes later; by then the chapter is published, or the first run's lease has
lapsed and the slot is free. A second publish of one chapter is already refused whole
(`ChapterAlreadyPublished`, `jobs.py:761-774`), so even a run that got past the slot by a bug pays but
never writes twice. The publish's lock moves from the reader's book (`jobs.py:754-757`) to the edition.

**Reading the notes so far, for the build.** Today the working copy is read through the reader's own
tenant role (`story_memory.py:134-142`). An edition's rows belong to the notes account, which a
reader's tenant session cannot read (section 4), so the worker role reads them, with the predicate
"chapter before *n*" written into the statement. Because the run only builds *n* when its reader has
finished *n*−1, everything the copy holds is at or before that reader's place: the build triggered by a
reader never holds notes of a chapter they have not finished. The copy checks each copied chapter's
text hash against the reader's copy, as today (`story_memory.py:207-212`), so a collision or a mixed
split fails closed.

**Failures and retries are unchanged, per edition.** Two runs failing on a chapter for a reason the
chapter causes publish it as an "unremembered" chapter (`companion/jobs.py:245,454-460`,
`story_memory.py:424-435`); three in a row stop the book with `too_many_unremembered`
(`hosted/jobs.py:50`, `companion/jobs.py:454-455,461-465`). The counts now read the edition's chapters
and all readers' failed runs of that edition and chapter (today one reader's, `jobs.py:568-577`), so
one bad chapter is paid for twice per file, not twice per reader. A stopped edition stays stopped
for every reader until the operator looks (today's library rule, open items line 259); there is no
other place a stopped book is retried.

**What a reader sees while a chapter is not ready.** The story screens show the notes up to
`min(place, what is built)` (`routes.py:286-290,327-328`), and the app refreshes as more arrives (ADR
0042 section 2). "I'm a little behind you. I'll catch up on the rest tomorrow."
(`frontend/src/reader/StoryMemory.tsx:68`) shows only when the reader's job waits for tomorrow
(`:70-90`, `memory.py:462-471,494-507`), unchanged. One screen change is needed: today a book with no
memory rows and no job answers 404 and the panel says "I don't keep a list of who's who for this book
yet" (`routes.py:318-319`, `StoryMemory.tsx:53-57`). Every book now has notes in principle, so that
answer must become the same empty cast for every book a reader has not finished a chapter of, whether
or not an edition exists (section 4, "what must answer identically").

### 4. The door stays the only way in

**Every path goes through it.** The chat (`stream_read_prefix` with a `HostedBookStore`,
`routes.py:222-229`), the home chat's answers about each book (`home_conversation.py:372,384`, the same
store), the four story screens (`routes.py:293-369`), progress (`companion/store.py:98-101`) and "take a
copy of my data" (`tenant/export.py:356-380`) all read a book's memory through
`HostedBookStore._load` (`memory.py:522-539`), which asks the door first. Today a book the door does not
serve falls back to the reader's own rows (`memory.py:529-531`, `_LOAD` at `:49-134`). Under this design
no reader owns memory rows, so the fallback is deleted and the door is the only reader of memory for
the web process.

**What the door changes.** In `0026:74-97`: the reader's book needs a fingerprint, not a free-library
tag; the edition is the notes account's live book with the same `file_hash` and `split_hash`. Nothing
else: the place (`0026:98-108`), the predicates and the hidden later ends (`0026:109-250`) and the key
rewrite (`0026:111-114`) stay as they are. One addition: for a live book of the reader with no edition
yet, the door returns one row with empty lists, the same shape as an edition read at place 0, so that
"no one has built this" and "someone has built this but you have not read any of it" are the same
answer.

**What in the database stops a reader's session reading shared notes any other way.**
1. Row-level security, forced on every memory table: a reader's session sees only rows whose owner is
   the reader (ADR 0041 section 2), and edition rows belong to the notes account.
2. The door is the one `SECURITY DEFINER` function, `EXECUTE` revoked from `PUBLIC` (`0026:254`) and
   granted to the tenant role alone (`tenant/grants.py:93-98`); it takes no place argument.
3. New, recommended: the tenant role loses every privilege on the memory tables (today `SELECT` and
   some `INSERT`/`UPDATE`/`DELETE`, `grants.py:23-32`). With no reader-owned memory and no corrections
   (section 7), nothing in the web process needs them once the readers of the reader's own chapters
   are changed (`tenant/repository.py:406-415,779-781`, `companion/store.py:103-112`). Then even a bug
   that forgot the owner predicate could not read an edition row from a reader's session. This is a
   layer added, not removed.

**Which protection carries the load, by case.** "Not built" is protection by the notes not existing;
"door" is protection by a guarded handover of notes that exist; "clamp" is the screens' own bound
(`routes.py:286-290`); "checks" are the chat's fixed name check, Jev and the second look (ADR 0042
section 3).

| Reader | Today (library book) | Today (upload, setting off) | This design |
|---|---|---|---|
| The furthest reader of a file, chapters past *n*+1 | door, clamp, checks (the whole book exists) | no notes at all; checks | **not built**, door, clamp, checks |
| The furthest reader, chapter *n*+1 | door, clamp, checks | no notes; checks | door, clamp, checks (**not built is gone for this one chapter**) |
| Any reader behind the furthest | door, clamp, checks | no notes; checks | door, clamp, checks (**not built is gone up to the furthest reader +1**) |
| A reader of a different edition | nothing of this edition can be reached | same | same: a different fingerprint never matches |

So, said in so many words: **for imported books this design relies on the door where today there is
nothing to protect.** The door is the protection free-library readers already rely on for the whole
book, with its tests (below). Per-reader memory (ADR 0042 (A)) would have kept "not built" for every
reader, at about $7.30 a novel per reader.

**The part of Mothli that answers a reader never receives later chapters.** The answering part is the
web process's request; it receives only the door's rows, bounded by that reader's place. The building
part is the worker, a separate process that answers no one: it receives one chapter's text (*n*, from
the triggering reader's own file) and the notes of chapters before *n*. With one ahead, chapter *n* may
be one the triggering reader is still reading. That text goes to the note-writing model only, as the
free library's whole-book build does today, and its notes reach any reader only through the door. The
rule that a skimmed page never reaches a model is about the reader's reading reaching the answering
model (`read_map.py:88-94,116-151`); the note-writer never receives anything about any reader.

**What one reader can learn about another: the honest limit.** Nothing a reader is sent states that
another reader has the same file. But speed can hint at it. A reader who marks ten chapters read gets
their notes at once if someone built them before, and over about half an hour if not; a reader whose
notes never wait while another's do can guess. For a free-library classic that says nothing worth
knowing. For a rare private file (a manuscript two people share) it can tell one of them that the other
read at least so far. ADR 0042 (line 276-279) rejected this signal for the library; the owner's
decision accepts it. Holding finished notes back to fake a build time would remove it, at the cost of
making every reader wait; this record does not propose it. **What I told the owner, "no reader ever
sees that", holds for everything shown, not for timing.** Question 5.

**What must answer identically**, whether or not another reader has built anything: the door's answer
at place 0 (above), the story screens' answer before the first finished chapter (section 3), the
correction route's refusal (section 7), the export (section 6), and any price or plan screen
(section 5). Each gets a test.

**The spoiler tests of the door today** (`backend/tests/hosted/test_library_story_memory_postgres.py`,
12 tests), and what each becomes:

| Today | Becomes |
|---|---|
| `:321` readers at 2, 5 and 10 are each handed only what is up to their place (raw function, adapter, superuser with the owner set) | the same, run once for a free-library add and once for an upload of the same file |
| `:402` no argument, device report or direct write moves the place; the door refuses the rest | the same for an upload; plus: a reader of a **different file** of the same title, a reader whose split differs, a removed book, an account pending deletion and the notes account itself get nothing |
| `:495` "I haven't read this" shrinks the answer, reading on grows it | the same, upload |
| `:536` a reader of the edition gets what local mode builds to their place | the same, the edition now built by readers' own runs, chapter by chapter |
| `:576` the chat's spoiler probes through the door | the same, upload, with two readers of one file at different places: the one behind is probed |
| `:632` a later name the writer remembers is left to Jev and the second look | the same |
| `:674` the edition tells no reader anything of another; its account is nobody's | widened: the door at place 0 is byte-identical with and without an edition; no edition row names a reader |
| `:743` the command builds a whole title, pays its own way | deleted with the command; replaced by the build tests of section 3 |
| `:817` three unremembered in a row stop a book | the same, counted per edition across readers |
| `:871` the library role is exactly what the command needs | deleted with the role |
| `:898` removing a book and erasing a reader leave the edition and take theirs | becomes the section 6 tests |
| `:940` the place follows the read map in every path, nothing else writes it | the same, upload |

**New spoiler tests for the new paths**, each written before the behaviour it guards (section 9):
two readers at chapters 2 and 5 of one imported file, the door read for each; a reader behind the
furthest reader, with the edition built to the furthest +1: nothing past their own place; the furthest
reader at *n* with *n*+1 built: chapter *n*+1 is not handed to them (raw function, screens, chat
probes); a reader of another edition of the same novel: nothing; a forged place (device report,
direct write to `bookmark`, a `memory_through` past every read page): the backstop holds for uploads;
a build triggered by reader A never holds a chapter A has not finished, apart from the one being built;
the chat's probes for the reader behind; an unread imported book whose edition others have built to
the end: the home chat and the book's chat get nothing from it (the owner's rule for unread books).

### 5. Money

**Who the spend is recorded against.** Options:

1. **The reader whose reading asked for the chapter pays, under their own caps.** The caps are $0.10 a
   reader a day and $2.00 a month, and $5.00 a day for everyone (`config.py:150-152`, defaults still to
   be confirmed by the owner). At about $0.09 a chapter that is one chapter a day and about 22 a month,
   with nothing left for the chat on those days. A reader of five chapters a day falls four chapters
   further behind every day. Only first readers would ever pay or wait, so the "catch up tomorrow" line
   and an empty chat allowance would tell a reader they are first. Not recommended.
2. **Mothli pays, recorded against the notes account and the edition (recommended).** The notes account
   has its own `owner_limits` row and its own day and month caps, exactly as the library account does
   today (ADR 0042 section 4, lines 446-451; `python -m app.hosted.limits set`); the run's calls are
   reserved through a `ChapterCatalog` (`companion/store.py:272`) bound to the notes account, so a chapter
   once admitted finishes as a reply does (`spend.py:274-275,300-320`), and the platform's day still
   counts every call. Every reader is treated the same: no reader's allowance is touched by notes, so
   nothing about spend differs between first and later readers. The ledger of a file's notes is one
   ledger, kept when readers leave. On top, two guards per reader (below).
3. **Mothli pays for notes the reader is shown; a reader pays for a book's notes as a purchase.** The
   pricing report (`reports/Mothli pricing and plans.md`, line 31) suggests selling story memory for
   an uploaded book at about $14.99 a book. Under shared notes, a price charged only when no edition
   exists would tell a reader whether someone else brought the same file, and the second reader would
   get for nothing what the first paid for. If the owner sells notes, the price must be the same for
   every reader of every book whether or not an edition exists: a price for access, not for the build.
   That is a pricing decision, not a design one (question 1).

**Arithmetic** (terra's list price, ADR 0042's measured $0.09 a chapter and about $7.30 for a novel of
*Pride and Prejudice*'s length; later chapters may cost more, ADR 0042 "least sure of" 1):

| | Option 1 | Option 2 |
|---|---|---|
| One new novel, read to the end by one reader | $7.30, from that reader's allowance over about 81 days | $7.30, once, from the notes account |
| The same novel, ten more readers of that file | $0 more, but only after the first reader finished | $0 more |
| A reader of five new chapters a day | $0.45 a day wanted, $0.10 allowed | $0.45 a day, within their guard |
| 100 readers a day each reading 3 chapters of books nobody else has | $10 wanted against $10 of reader caps, but the platform's $5 day stops it at about 55 chapters | about $27 a day; needs a notes ceiling of its own and a higher platform day |

**Guards, under option 2.** In plain words: a cap on how many chapters any one reader can be the first
to reach, so that one person cannot make Mothli spend a lot.

- **Per reader:** at most 12 new chapters a day and 120 a month that this reader's runs build (counted
  from their own `story_memory` jobs that published, which the purge erases with them). That is at most
  about $1.08 a day and $10.80 a month per reader, enough for a fast reader of one new novel a week.
  Reading chapters someone else already built never counts. Past the guard, the run waits for the next
  UTC day as a capped reader's job does today (`hosted/jobs.py:296-356`), and that reader sees "I'm a
  little behind you. I'll catch up on the rest tomorrow."
- **Per reader, new files:** at most 5 new editions a month (a reader is the first to chapter 1 of a
  file). Importing costs nothing (chapter 1 waits for a read page, section 3), and the upload limits
  already bound how many books a reader keeps (`tenant/repository.py:1453-1460`).
- **For everyone:** a notes day ceiling, recommended $10 (about 110 chapters), as the notes account's
  day cap; the platform day rises to $15 so that notes and chat do not starve each other (today one
  $5.00 day covers both, `config.py:152`). At the ceiling every new chapter waits for tomorrow; chapters
  already built are served as before.

**The abuse case, worked through.** Someone imports 50 files and clicks "I've read this" through all
of them. Without guards: 50 × $7.30 = $365. With them: 12 chapters, $1.08 that day, $10.80 that month
at most. With 30 such accounts: $32 a day, stopped at $10 by the ceiling. Every account needs a
sign-in at the sign-in service (ADR 0041), which bounds how many can be made cheaply, but not to zero:
the ceiling is what bounds the total.

**A reply under way always finishes** and **a chapter once admitted finishes**: both unchanged. The
claim's per-reader dollar check (`hosted/jobs.py:274-356`) must stop applying to `story_memory` jobs
(their spend is no longer the reader's) and apply the chapter guards instead.

**What "about 9 cents a chapter once per distinct file" really means.** True in the usual case, with
these exceptions: a chapter that fails its review is paid twice before it is kept unremembered (section
3); a run that dies mid-chapter pays that chapter again (ADR 0042 section 6); at most one chapter per file
may be built and never read (one ahead); two editions of one novel pay twice; and notes erased when the
last holder is gone (section 6) are paid again if the file comes back.

**What is a pricing decision, not a design one:** the guard numbers, the notes ceiling, whether the free
plan and the paid plan differ in how many chapters a reader may be first to, and whether notes are ever
sold. None of them changes the design: they are rows of `owner_limits` and three settings.

### 6. Deleting

**Removing a book** erases, today, every row about that book's reading, and its own memory through the
chapter cascade (`0041:868`, migration `0024`). Under this design the reader owns no memory rows, so
removal erases their book's reading, their jobs are cancelled (ADR 0042 section 6, "Leases"), and
the book row is kept as a bare id with the fingerprint cleared (`repository.py:1816`): their link to
the edition is gone. **Deleting an account** erases every owner row after 30 days (`0041:867`), the same.
Neither touches the edition: it is the notes account's, which the purge never erases (ADR 0042
section 7, first row; `0026:10-12`).

**The edition stays while any reader holds the file**, meaning a live `books` row with the same two
fingerprints whose account is not pending deletion. **When the last holder is gone, the edition is
erased 30 days later (recommended).** The purge, which already runs daily and can read every owner's
rows (`0041:867` item 4), marks an edition with no holder and erases it (the `books` row of the notes
account; every memory row follows through the cascade of migration `0024`) once 30 days have passed
with no holder; a holder coming back clears the mark. Why: notes are summaries of a book's story; for a
private file (a manuscript, a book someone wrote) they are derived from content nobody keeps in Mothli
any more, and "we keep nothing derived from a book nobody has" is the simplest rule to state in the
privacy text. Cost: a file brought back after that pays again, about $7.30 for a whole novel, spread
over its chapters. The alternative (keep editions of free-library files for good, since they are public
domain) is question 2.

**"Take a copy of my data"** keeps giving each book's cast and story so far at the reader's place, read
through the door (`tenant/export.py:356-380`), unchanged. It never says whether the notes were built from
this reader's reading or someone else's. One added sentence in its explanation (`export.py:108-113`): "Mothli's
notes on a book's story are made once for each exact book file and used for everyone who has it; this
copy holds them only up to where you are."

### 7. Corrections

Today a reader can correct their own book's memory (`POST /api/books/{id}/memory-corrections`,
`tenant/api.py:935-959`, `repository.py:1159-1420`), and a free-library book answers "Mothli's notes on
free-library books are prepared once for everyone, so they can't be corrected here." (`repository.py:59-60,
1178-1184`).

**Decided: no reader corrects shared notes, for any book.** A correction to shared notes would change
what every other reader of that file is shown, from one reader's word, unchecked; it would be one
reader's own content inside notes everyone gets; and a correction route that worked for some books and
not others would tell a reader whether their file is shared. The refusal becomes one line for every
book: "Mothli makes its notes on a book once for that book file, so they can't be corrected here." A
"tell Mothli this looks wrong" report to the operator, without changing anything, could come later and is
not part of this record.

What it removes: the hosted correction write path and its history (`repository.py:1017-1420`, about 400
lines), the hosted `GET /memory` (`api.py:900-913`, `repository.py:904-1016`), `GET` and `POST
/memory-corrections` (`api.py:916-959`), three entries of `tenant/endpoints.json` (`:210-226`), the
correction's lock against a publish (`repository.py:1171-1177`), and the tenant role's write grants on
memory tables (`grants.py:26-32`). The publish's "the memory moved since it was copied" check
(`story_memory.py:17-21`, `jobs.py:775-779`) loses its only cause but stays: it is cheap, and it also
catches two publishes that a bug let through. The app's correction screen is local mode's and stays
there (`frontend/src/api.ts:903-910`); in hosted mode the routes answer 404, which the app already treats
as "not here".

### 8. What gets simpler

Found by search on e4df206:

| What goes | Where | Tests |
|---|---|---|
| The operator's build command, the library role and its runbook step | `app/hosted/library_memory.py` (350 lines); `LIBRARY_TABLE_PRIVILEGES` and the library account's grants (`tenant/grants.py:85-110`); `scripts/hosted_roles.py:36,96-98,245,255`; `deploy/compose.yaml:67,184-190`; `deploy/.env.example`; runbook step 4b (`docs/runbooks/hosted-deployment.md:182-210`) | `test_library_story_memory_postgres.py::...:743` and `::...:871` go (no spoiler test); `test_migrations.py` loses the library role's service-table line |
| `HOSTED_READER_STORY_MEMORY` | `config.py:155-158,222-223`; `companion/routes.py:85`; `companion/jobs.py:421-427`; `deploy/app.env.example:75-79` | `test_companion_stream_settings.py:167-179` (the setting's check), `test_platform_pays.py:267` (one parameter), `test_story_memory_jobs_postgres.py:74` (fixture) and `::...:821` "with a reader's own story memory off nothing is made or spent" (not a spoiler test; its place is taken by "importing a file builds nothing", section 3) |
| The library account's special cases | `hosted/jobs.py:579-586` (`is_library_account`); `companion/jobs.py:425-446` (the library branch and the "door serves this book" exit); `StoryMemoryQueue.ensure`'s exception (`companion/jobs.py:110-118`); the `gutenberg:` tag in the door (`0026:81`) | the "refuse a reader's book the door serves" cases in `test_story_memory_jobs_postgres.py` change from "no job" to "builds the edition" |
| The reader's own memory path in the web process | `_LOAD` and the fallback (`memory.py:47-134,529-531`); the `shared` flag (`memory.py:517-520`); progress's reader-rows branch (`companion/store.py:103-112`) | none deleted; `test_companion_story_memory_postgres.py` seeds edition rows instead of reader rows |
| Reader corrections (section 7) | as listed in section 7 | `test_correction_logic_audit.py` (1 test), `test_tenant_postgres.py:911` "reader memory corrections are owner-scoped and frontier-bounded", `test_story_memory_postgres.py:257` "a correction during extraction is respected" |

**About the one spoiler test that goes:** `test_tenant_postgres.py:911` checks that a correction can
neither be made nor shown past the reader's frontier. It guards a feature that goes; nothing it protected
remains (ADR 0043's rule for R3 to R6). Every door test stays or is widened (section 4). No other spoiler
test is deleted.

**What stays, and is not duplicated:** one job kind, one handler, one publish, one door, one set of
spoiler checks. The per-reader memory path and the operator's path become one path: "the next chapter
of this file's edition, built from the copy of the reader who reached it".

### 9. Steps

Small pull requests, each green on its own (`ruff check app tests scripts`, the hosted Postgres suite
locally), stand-in models only, tests in the same pull request written before the code they test,
smallest risk first. Migrations from **0031**.

1. **The split fingerprint** (migration `0031_book_split_hash.sql`: `books.split_hash`, set by
   `store_epub`; the door also requires it to match). Proves: two imports of one file have equal
   fingerprints; a changed split gives another; the door refuses a reader whose split differs from the
   edition's (the library tests, `:402`, gain the case). Stricter only; nothing else moves.
2. **The door for every book** (migration `0032_notes_account.sql`: rename `library_account` to
   `notes_account`, create it in the migration, the unique edition index; replace
   `library_story_memory` by `story_memory_door(book)` matching on fingerprints only, and returning the
   empty row for a book with no edition). Tests first: the library door tests run for uploads too
   (section 4 table), the new two-readers, behind-the-furthest, different-edition and place-0-identical
   tests, against seeded edition rows. Nothing builds editions for uploads yet, so readers see nothing
   new.
3. **Builds into the edition** (migration `0033_one_build_per_chapter.sql`: the unique slot on `jobs`;
   worker grants to create an edition row and read edition rows). Changes `story_memory.py` (copy read
   by the worker role bounded below *n*, publish to the edition), `hosted/jobs.py`
   (`begin_story_memory_chapter` takes the slot, the publish's lock), `companion/jobs.py` (one path, no
   library branch). Tests first: two readers asking for one chapter at once make one build and one
   spend; a reader's run reads only their own file and only notes before *n*; no edition row names a
   reader; the failure and "three in a row" tests counted per edition. The library command still works
   (its account is the notes account) until step 6.
4. **Trigger, one ahead, money** (no migration: the guards are settings and `owner_limits` rows).
   Changes `routes.py` (no setting), `companion/jobs.py` (target `finished_through + 1`, 0 before a read
   page), the claim's cap check for `story_memory`, `config.py` (guards, ceiling; `HOSTED_READER_STORY_MEMORY`
   removed), `spend.py` callers (spend under the notes account). Tests first: the target never passes
   `finished_through + 1`; a skimmed page and an import build nothing; the guards stop the run whole
   and move it to tomorrow; a capped reader's chat and their notes are independent; the abuse case.
   Moves `::...:821`.
5. **Screens say the same for every book** (no migration). Changes `routes.py:318-319` and the panels'
   empty line. Tests first: a reader before their first finished chapter gets the same answers with and
   without an edition; frontend tests for the new line.
6. **Delete the operator's path** (no migration needed for the code; the library role is dropped by the
   role script). Deletes the command, the role, the runbook step, `is_library_account`. Moves `:743`,
   `:871` out. Proves: the role check and `test_migrations.py` pass without the library role.
7. **Corrections refused for every book, the tenant role loses the memory tables** (migration
   `0034_no_reader_memory_writes.sql` if any grant lives in SQL; the grants list otherwise). Deletes the
   section 7 code and the three tests; changes the position readers of the reader's own chapters.
   Proves: the grants test with the narrower list; the door tests unchanged.
8. **Erasing an edition nobody holds** (migration `0035_edition_holders.sql`: the "no holder since"
   mark). Changes `hosted/purge.py`. Tests first: removal and erasure leave the edition while another
   reader holds the file; the last holder gone, the edition is erased after 30 days and not before; a
   holder coming back clears the mark; the export is unchanged.
9. **Words** (no code): ADR 0017 section 3 and ADR 0042 sections 3 and 6 marked as amended; the privacy
   and terms sentence below; open items updated.

Steps 1 and 2 can be built side by side; 3 waits for 2; 4 and 5 wait for 3; 6, 7 and 8 wait for 4 and
can be built side by side.

- Built on 18 November 2026 in PR #122: step 1, the split fingerprint, as designed, except (1) the
  list is hashed as compact JSON (`[[number, file part, text SHA-256], ...]`, UTF-8) by
  `split_hash_of`, beside `content_hash_of` in `app/ingest/extraction/chapter_text.py`; it holds no
  content-policy or splitter version, as section 1 says; (2) rows already there get NULL, meaning
  "not known", and a check allows only a SHA-256; the door refuses a reader's book with no
  fingerprint, and an edition with none matches nothing; (3) the door keeps its `gutenberg:`
  condition until step 2; only its match changed (three lines); (4) removing a book clears the
  fingerprint with the file hash; (5) the `:402` test is unchanged: the different-split and unknown
  cases are a test of their own, with a copy whose splitter numbers every chapter one on;
  (6) `create_uploaded_book` requires the fingerprint; (7) the copy of one's data does not carry
  it, as it does not carry the file hash; (8) unverified point 3 below is checked: one file-driven
  and one anchor-driven made-up book give one fingerprint in four separate processes with
  different string-hash seeds (no real book file is in the repository to try).
- Built on 18 November 2026 in PR #123: step 2, the door for every book, as designed, except
  (1) **the unique edition index is not built**: an index's condition cannot name the notes
  account without writing its id into the statement, which needs a procedural block, and the
  migrations may not contain one (`test_migrations.py` refuses the word `BEGIN` in any of them);
  nothing creates editions side by side before step 3, so step 3 decides it (a marker column on
  `books`, or another way); (2) the door's body differs from 0031's in six places only, checked
  line by line by `test_migrations.py`: the reader's columns lose `source_kind`, the function's
  own name, no `gutenberg:` condition, `notes_account` for `library_account` (twice), no
  `source_kind` match, and `FROM reader CROSS JOIN place LEFT JOIN edition ON true` for the empty
  row; a reader's book with no fingerprint still gets no row; (3) the account the migration
  makes keeps the operator command's name, "Mothli library", and the command, its role and
  `is_library_account` work on `notes_account` until step 6; (4) the web process takes a book as
  served by the door when the door hands anything (`door_served`); where it hands nothing (no
  edition, or place 0) the reader's own rows answer as before, and a book with both is answered
  by the door alone; the progress screen, the correction refusal and the reader's story memory
  queue use the same rule; (5) so a free-library reader before their first finished chapter is
  now answered as for any book with no notes ("I don't keep a list of who's who for this book
  yet", and no story in their copy of their data) rather than with an empty cast: the same with
  and without an edition, until step 5 changes that line for every book; (6) the door does not
  read an account's state: an account waiting to be erased is refused by its sign-in, and its
  copy of its data holds nothing past its place; (7) the editions in the tests are built by the
  operator's command, chapter by chapter, not by readers' runs; (8) the correction refusal for an
  upload the door serves still gives the free-library line; no upload is served before step 3,
  and step 7 replaces the line.

**The sentence for the terms and privacy pages** (replaces the free-library one, open items line 254):
"Mothli's notes on a book's story (who's who and what has happened, chapter by chapter) are made once for
each exact book file and used for everyone who has that same file, whether they uploaded it or took it
from the free library. Each reader is shown them only up to where they have read. Nothing of yours is
shared: not your highlights, notes, conversations or reading position, and not your copy of the book.
When nobody has the file any more, its notes are deleted 30 days later."

## What I told the owner, checked

- **Spoiler protection unchanged: the door, by the reader's own place, and the answering part never
  receives later chapters.** True, with one layer change he should hear: for imported books, and for
  the chapter after the furthest reader of any book, protection rests on the door alone, not on the
  notes not existing (section 4 table). For free-library books there is less built ahead than today.
- **About 9 cents a chapter once per distinct file.** True in the usual case; the exceptions are in
  section 5.
- **Mothli knows internally that two readers have the same file; no reader ever sees that.** True for
  everything a reader is shown; not fully true for timing (section 4, "the honest limit").
- **The first reader of a chapter may wait briefly.** With one ahead, the furthest reader usually does
  not wait at all; they wait after jumping ("I've read this" on many chapters) and past their guard.
- **The terms page needs a sentence.** Drafted above.

## Consequences

- Every book, free-library or imported, gets the cast and the story so far, built as readers reach it,
  once per file. The operator builds nothing and chooses no titles.
- One path builds notes and one door hands them out; the per-reader memory path, its setting, the
  operator's command and role, and reader corrections go.
- Spend for notes is the notes account's, bounded per reader by chapter guards and for everyone by a
  notes ceiling; no reader's allowance pays for notes.
- ADR 0017 section 3 is amended for every file; ADR 0042's reasons for not building on demand are
  accepted as costs (section 4).
- Mothli never keeps a book file that no current reader holds, and erases notes 30 days after the last
  holder is gone.

## Questions for the owner

1. **Who pays for the notes, and under which cap?** Recommended: Mothli pays, from a notes allowance of
   its own, $10 a day for everyone, and each reader can be first to at most 12 new chapters a day and 120 a
   month (about $1.08 and $10.80 at most), with the overall daily limit raised from $5 to $15. If you
   later sell plans, the price of notes must be the same for every reader whether or not someone else
   already has the book.
2. **When the last reader with a file is gone, do its notes go?** Recommended: yes, 30 days later, for
   every book. Alternative: keep notes on free-library files for good (they are public domain), and pay
   once less when someone brings that file back.
3. **Is "one chapter ahead" acceptable?** Recommended: yes. Readers almost never wait; the price is that
   the notes on the next chapter exist before anyone has finished it, guarded by the door alone, and at
   most one such chapter per file may be paid for and never read.
4. **May readers correct shared notes?** Recommended: no, for every book; one line says why. A "this looks
   wrong" report to you could come later.
5. **Is the timing hint acceptable?** Someone who marks a rare book read and gets its notes instantly could
   guess that somebody else has the same file. Recommended: accept it, as the free library's door already
   does; hiding it would mean making everyone wait.

## What this record could not verify

1. That Postgres accepts the partial unique index on two `payload_metadata` fields of `jobs`, and exactly
   when an expired lease leaves `running` so the slot frees (the claim's handling of expired leases was not
   read in full).
2. That nothing else in hosted mode reads the reader's own memory rows besides the places cited in
   section 4 point 3 (found by searching for the table names in `backend/app`, not by running the suite
   with the grants removed).
3. That `segment_for_ingest` splits one file the same way every time with one version of the code (the
   split fingerprint assumes it; not tested here).
4. The cost of a whole book and of later chapters (ADR 0042 "least sure of" 1), the real time a chapter
   takes on a paid account (whether "one ahead" is enough for fast readers), and how often terra rejects
   a chapter.
5. Whether the purge role can read the notes account's rows and `books` across owners for the holder
   check without a new privilege (its privileges are read from the catalogue for tables with `owner_id`,
   `0041:867`; `notes_account` deliberately has none).
6. How many readers would share files in practice: the savings of this design depend on it, and nothing
   has been measured.
