# ADR 0045 — Two free weeks, then one paid plan: reading stays free, Mothli is the plan

**Status:** Proposed (2026-11-20). Step 1 is built (see below); nothing else is. The owner's
decisions in "What the owner decided" are settled; the numbers and the choices in "Open points for
the owner" are his to make.

**Step 1 built (2026-11-20).** Migration `0037_reader_plans.sql`: `reader_plans` (`owner_id`,
`free_started_at`, `updated_at`; row-level security forced; the tenant role holds `SELECT` only),
`start_free_weeks()` (no argument, a security definer, stamps the transaction's reader once from
the database clock) and `reader_plan_state(reader, free_days, free_usd)`, which runs as its caller
and answers `new`, `free` or `ended` with `time` or `ceiling`. Settings `HOSTED_FREE_DAYS` (14) and
`HOSTED_FREE_USD` (2.10). The two ask routes call `start_free_weeks()` once the request is valid,
before anything is read or reserved; no other route starts the clock. `reserve_cost` applies the
free ceiling in place of the reader's day and month while the free weeks last; at the ceiling the
reader sees section 6's ceiling line without its last sentence and without a button. Two choices
made in building it:

- **An operator's own caps win.** A reader whose `owner_limits` row sets their own day or month cap
  is held by those caps exactly as before, in the free weeks or not, and the free ceiling is not
  applied to them. The free weeks replace only the defaults. So a reader an operator has slowed
  stays slowed, and the owner and testers keep the caps he raised for them until gifts (step 2).
- **The clock starts once the question is one the server will try to answer**: the chat is on
  and the request names a read position. A question about a book that is not the reader's still
  starts it (it is answered 404 afterwards).

Left for later steps: the gift and Paddle columns and states, the `billing` role, the session's
`mothli`, the `402 mothli_plan` gate and its invitations (step 5; until then a reader past the
ceiling can still open the cast and "Story so far"), the start call from the cast and "Story so
far" (step 5), background work by state (step 6), "your plan.json" in the copy of one's data (step
7; `reader_plans` is listed as left out of the copy until then), and open point 4 (the platform's
day in the free weeks).

File and line references are to `main` at 2475b6f (PR #136). Nothing in this record was run against a
database, a server, a model or Paddle. Every claim about the code comes from reading the lines cited
next to it. Every claim about Paddle comes from Paddle's own pages, read on 20 November 2026, linked
next to the claim. No code, test, migration or setting changes with this record.

## Decisions, in short

1. **Reading is free forever.** Books, the place in each book, highlights, notes, bookmarks and the
   free library never need a plan.
2. **Mothli is the plan.** The chat inside a book, the home chat, the cast and "Story so far" need
   either the two free weeks or the paid plan. One plan, paid monthly or yearly, through Paddle.
3. **The two free weeks start the first time a reader uses Mothli**: their first question, or the
   first time they open the cast or "Story so far" themselves. Not at sign-up. The server's clock,
   stamped once, kept per account, never reset by removing a book.
4. **The free weeks have one hidden ceiling, in dollars of real chat spend: $2.10** (about 100
   replies). No "resting until tomorrow" stop during the free weeks.
5. **A paying reader has two hidden guards**: a day guard that shows today's resting line, and a
   month ceiling. Proposed: $0.60 a day and $4.00 a month. No number is ever shown.
6. **No counter, anywhere, ever, and never the word "unlimited".** The only words about the free
   weeks are a friendly hello at the start and one gentle note in the last two days.
7. **A reader whose free weeks are over keeps everything.** Conversations, their own notes and what
   Mothli remembers of them are kept, not deleted. Subscribing picks up where they left off.
8. **Only the server decides who may use Mothli**: from its own clock, from Paddle messages whose
   signature it has checked, and from the owner's own command for gifts and test accounts.
9. **Plans never change what Mothli reads, searches or sends.** The plan check stands in front of
   the conversation and only says yes or no. Section 9 shows this layer by layer.
10. **Background work follows the state**: as today for readers in the free weeks and paying
    readers; a bounded warm-up for readers who have not started; none for readers whose free weeks
    are over. Readers are never told about any of it.

## What the owner decided (settled; this record does not reopen them)

- He pays for the AI. Readers never use their own AI account (ADR 0043). Readers pay him, through
  Paddle, the only payment company.
- Two free weeks of the whole of Mothli, no counter shown, a hidden ceiling of about 100 questions.
- After the free weeks, a reader who has not paid keeps reading for free, forever.
- His words on counters: "i want users to feel like they can talk to mothly all the time i dotn watn to
  instill fear on them that hey u have only this many questions". So no counter, and never
  "unlimited", which would be untrue.
- One plan, monthly and yearly. Prices live in Paddle and in settings, not in code. Proposed, not
  final: $9.99 a month, $79.99 a year.
- Per-chapter notes are never sold, metered or shown as a limit; nothing shown may imply anything
  about another reader (ADR 0044, the owner's rule).
- Spoiler safety is not negotiable, in every book and across books.
- Mothli's voice: a playful, helpful friend. English only. The reader "knows nothing how AI works".
- A reply that began under a ceiling is finished (`spend.py:17-21`, `ReplyCatalog` in
  `backend/app/hosted/companion/store.py:242-255`). Kept.

## Context: what exists today

- **One allowance per reader, in dollars, for their own conversation only.** `reserve_cost`
  (`backend/app/hosted/companion/spend.py:303-409`) checks, under the reader's spend lock, their UTC
  day, their UTC month, an optional lifetime cap, their calls in flight, then the platform's day.
  Defaults: $0.10 a day and $2.00 a month for a reader (`spend.py:108-109`, settings
  `HOSTED_READER_USD_PER_DAY` and `..._PER_MONTH`, `backend/app/config.py:150-151`), $15.00 a day for
  everyone (`config.py:152`). A reader's own `owner_limits` row can override the day, month and
  lifetime caps (`spend.py:148-163`, table `backend/app/hosted/schema/0011_owner_limits.sql`).
- **At about 2.1 cents a reply, $0.10 a day stops a reader at about their sixth question.** The line
  they see is `_DAY` (`spend.py:212-213`): "We’ve talked a lot today, so I’m resting until tomorrow.
  Your book and your place are right here." This is the problem the first step below removes.
- **Background work is Mothli's cost, never the reader's allowance** (ADR 0041 section 6 as amended
  20 November, PR #134). Per-reader chapter notes (about 0.7 cents a chapter) and recollections are
  held by per-reader guards ($1.00 a day, $5.00 a month) and a budget for all readers ($3.00 a day)
  (`config.py:153-164`). Story memory (7 to 9 cents a chapter) is built once per identical book file
  and paid by the notes account under a $10 day ceiling, with per-reader guards on how many new
  chapters one reader's reading may be first to (30 a day, 300 a month, 5 new files a month)
  (ADR 0044 section 5, `config.py:165-180`, `backend/app/hosted/companion/jobs.py:337-345`).
- **The worker already holds a reader's background work by state of their spend**: the claim moves a
  capped reader's chapter notes and recollections to the next UTC day
  (`backend/app/hosted/jobs.py:400-416`).
- **The app learns at sign-in whether this server has Mothli's chat at all** (`"chat"` in
  `GET /api/auth/session`, `backend/app/hosted/auth/api.py:226-243`). There is no notion of a plan.
- **Who can write what.** The web process talks to the database as the tenant role, under row-level
  security, with the reader's id set per transaction. The tenant role may only read `owner_limits`
  (`backend/app/hosted/tenant/grants.py:30`). Roles are made by `backend/scripts/hosted_roles.py:84-96`
  (auth, tenant, worker, audit, operator, purge).
- **The app asks for the cast and the story by itself**, not only when the reader opens them: the
  reader page fetches the cast to name people (`frontend/src/reader/ReaderPage.tsx:1743-1755`) and the
  story notes after each finished chapter, for the corner note "That’s … done! Want to hear what I
  noticed?" (`ReaderPage.tsx:1634-1657`). This matters for section 2.
- **No terms, refund or privacy page exists in the app** (searched `frontend/src` and `docs`). Paddle
  needs all three before it approves the site (section 10).

## Decision

### 1. The states of a reader

| State | Who | Mothli | Background work |
|---|---|---|---|
| **New** | Signed up, has never used Mothli | Open; using it starts the free weeks | Warm-up, bounded (section 5) |
| **Free weeks** | Within 14 days of first use and under the free ceiling | Open, no day stop | As today |
| **Ended** | Free weeks over, or free ceiling reached, and not paying | One invitation in its place | None |
| **Paying** | A live Paddle subscription, or a gift from the owner | Open, hidden day and month guards | As today |

Inside **Paying** there are three edge cases, each taken from Paddle's own statuses
([get a subscription](https://developer.paddle.com/api-reference/subscriptions/get-subscription);
[provisioning guide](https://developer.paddle.com/build/subscriptions/provision-access-webhooks)):

- **Cancelled, paid to the end of the period.** Paddle keeps the status `active` with a scheduled
  change until the period ends: "The subscription keeps its current status until the next billing
  date" ([cancel subscriptions](https://developer.paddle.com/build/subscriptions/cancel-subscriptions)).
  Paying until then. The account sheet says when it ends.
- **A payment failed, Paddle is retrying.** Status `past_due`. Paddle's guide says to keep access and
  show a way to the customer portal. Paddle retries up to seven times over 30 days, then cancels
  ([default payment recovery](https://developer.paddle.com/changelog/2025/default-dunning-payment-recovery)).
  Paying during the retries; Ended when Paddle cancels.
- **Paused.** Paddle has a `paused` status; Mothli never offers a pause. Treated as Ended.

A reader who was paying and stopped is **Ended**, never back in the free weeks: the free weeks happen
once per account.

**Where it lives.** One new table, `reader_plans` (migration `0037`), one row per account, created on
first need (no row means New):

- `free_started_at`: when the free weeks began, from the database clock. Set once.
- `gift_until`: the owner's gift (section 8). NULL for none; a far date for "for good".
- `paddle_customer_id`, `paddle_subscription_id`, `paddle_status` (`active`, `trialing`, `past_due`,
  `paused`, `canceled`), `period_ends_at`, `ends_at` (the scheduled cancel's date, if any),
  `paddle_occurred_at` (the time of the newest Paddle message applied), `updated_at`.

The state is never stored as a word. It is worked out each time by **one SQL function**,
`reader_plan_state(owner, free_days, free_usd)`, from the row, the clock and the reader's own chat
spend since `free_started_at` (the same ledger `_spent` reads, `spend.py:78-94`). The session, the
routes, the spend check and the worker all call it, so they cannot disagree. It answers `new`,
`free`, `ended` (with why: `time`, `ceiling` or `plan`) or `paying`, plus the dates the screens need.

**Who may write it.**

- **The reader's session may not.** The tenant role gets `SELECT` only, under row-level security,
  as for `owner_limits`. It may execute one function, `start_free_weeks()`, which stamps
  `free_started_at = now()` for the reader the transaction is set to, only where it is NULL. It takes
  no argument, so no reader can pick a date or another account.
- **Paddle's messages**, through a new database role, `billing`, used only by the Paddle message route
  and the subscribe and cancel routes (section 7). It holds `SELECT`, `INSERT`, `UPDATE` on
  `reader_plans` and `SELECT`, `INSERT` on `paddle_events`, nothing else. It does not bypass row-level
  security: it sets the account from the checked message, as the tenant role does from the session.
- **The owner's gift command**, through one function only the operator role may execute
  (section 8).
- Nothing else. The worker reads the state; it never writes it.

Erasing an account erases its row (the purge takes every table with an `owner_id` column,
`backend/app/hosted/purge.py:93-96`). The copy of one's data (`backend/app/hosted/tenant/export.py`)
gains "your plan.json": when the free weeks began, and the plan's status and dates.

### 2. When the two free weeks start

**Recommended: at the reader's first use of Mothli** (the planner's pick): their first question, in a
book, on the home screen or about a selected passage, or the first time they themselves open the cast
or "Story so far".

How the server knows:

- **A question** is a `POST` the server already checks (`backend/app/hosted/companion/routes.py:218-248`
  and `:421-439`). The route calls `start_free_weeks()` before anything else. No change in the app.
- **Opening the cast or "Story so far"** cannot be read off the server's routes, because the app
  fetches both on its own (Context, last bullet). So the app sends one new call,
  `POST /api/reader/mothli/start` (with the usual sign-in and CSRF check), when the reader opens one
  of those panels by their own tap. The automatic fetches never start the clock.

The three choices weighed:

| Start at | For | Against |
|---|---|---|
| **Sign-up** | Nothing to build: `users.created_at` exists (`0001_identity_and_accounts.sql:6-16`) | A reader who signs up and comes back a month later finds the free weeks gone, at exactly the moment they would have tried Mothli. Loses the best readers to try it on |
| **First book opened** | Close to the moment of interest | Opening a book is a plain read (`GET`), which should not write. Most readers open a book on the day they sign up, so it is nearly sign-up. Someone who reads for weeks before trying Mothli still loses the taste |
| **First question only** | Cleanest: a `POST` the server already checks, no app change | The cast and "Story so far" stay open forever to a reader who never asks. Those panels cost story memory (section 5), not chat, so this is a slow leak, and it breaks "all of Mothli needs the plan" |
| **First use** (recommended) | Every reader gets the full two weeks when they actually try Mothli | One small new call from the app. A modified app could skip it and keep the panels open; not worth guarding against |

Fixed by the server: the clock is the database's `now()`, stamped once. Removing a book, clearing a
conversation or signing in on another device changes nothing. Deleting the account and making a new
one does (section 11).

### 3. The free ceiling

- **Counted in dollars of real chat spend**, as the existing machinery counts: the reader's own
  conversation, never background work (`spend.py:24-31`). From `free_started_at` on.
- **$2.10 for the whole free weeks**, a setting (`HOSTED_FREE_USD`, default 2.10), with
  `HOSTED_FREE_DAYS` (default 14). About 100 replies at 2.1 cents. The number of replies is an
  estimate: the code's own limits allow up to $0.33 for the longest reply
  (`docs/product/companion-open-items.md:194`), so a reader who asks very long questions gets fewer.
- **No day stop and no month stop during the free weeks.** The planner's pick, and this record agrees:
  a reader who talks all of their first day is the reader most likely to pay, and today's $0.10 day
  stops them at about the sixth question. The ceiling alone bounds the cost of a sign-up. The calls in
  flight stay limited (a reader waits for the last reply), and the platform's day stays (Open point 4).
- **Where it hooks in:** `reserve_cost` (`spend.py:347-381`). It already reads the reader's policy and
  spend under their lock. With the plan: in the free weeks, the day and month caps are not checked;
  the free ceiling is, in the same transaction. A reply admitted under the ceiling finishes, as today.
- **What the reader sees at the ceiling, before day 14.** It must not say the free weeks are over,
  because they are not. The line (section 6, "Ended at the ceiling"): "We’ve talked so much these first
  days that your free start with me is all used up. That’s a good sign! …". After day 14 the same reader
  sees the "time" wording instead.
- **Nothing warns a reader that the ceiling is near.** A warning would be a counter in disguise.

### 4. The paid allowance

**No number shown, ever.** Two hidden guards, reusing the existing settings and lines:

- **A day guard**: `HOSTED_READER_USD_PER_DAY`, proposed $0.60 (about 28 replies). Past it, the reader
  sees today's line, unchanged: "We’ve talked a lot today, so I’m resting until tomorrow. Your book and
  your place are right here." (`spend.py:212-213`).
- **A month ceiling**: `HOSTED_READER_USD_PER_MONTH`, proposed $4.00 (about 190 replies). Past it, the
  reader sees today's month line, unchanged: "We’ve talked our way through this month, so I’ll be back at
  the start of the next. Your book and your place are right here." (`spend.py:214-215`). No number, no
  date of a reset counter; "the start of the next" is the 1st of the UTC month, as the code counts.

Once the gate (step 5) is in, these two settings apply only to paying readers: readers in the free
weeks have the free ceiling instead, and New and Ended readers cannot start a reply. The names stay,
so the startup check that the operator has set them (`config.py:281-293`) keeps working.

**The arithmetic.** From the owner's pricing report (kept out of git): $9.99 a month leaves about **$7.33**
after 20% VAT included in the price and Paddle's 5% + $0.50; $79.99 a year leaves about $62.16, or
**$5.18 a month**. That is the cautious case; a sale with less tax leaves more. Background work for a
reader who reads 60 chapters a month is about $0.42 in chapter notes. Story memory is shared per file;
see the warning below.

| Month ceiling | About | Left of $7.33 (monthly) at full use | Left of $5.18 (yearly) at full use |
|---|---|---|---|
| $3.00 | 143 replies | $7.33 − 3.00 − 0.42 = **$3.91 (53%)** | $5.18 − 3.42 = **$1.76 (34%)** |
| **$4.00 (proposed)** | 190 replies | $7.33 − 4.42 = **$2.91 (40%)** | $5.18 − 4.42 = **$0.76 (15%)** |
| $5.00 | 238 replies | $7.33 − 5.42 = **$1.91 (26%)** | $5.18 − 5.42 = **−$0.24 (a loss)** |

"Full use" is the worst case: a reader who talks to the ceiling every month. Most will use far less. A
day guard of $0.60 means the month ceiling can only be reached by talking a lot on at least seven
days. Recommended: $0.60 a day and $4.00 a month, then tune both with real use.

**A warning about story memory, from ADR 0044, not changed here.** A paying reader who imports novels
nobody else has is the first to build their story memory: about $4 to $5.40 a novel. The existing
guards let one reader be first to up to 300 chapters a month, about $24 to $27, three times what the
monthly plan brings in. Readers of free-library books share one build and cost almost nothing. This is
the one cost a plan does not bound. It stays as ADR 0044 decided; Open point 5 asks whether to tighten
it.

### 5. Background work by state

| State | Chapter notes and recollections | Story memory |
|---|---|---|
| Free weeks, Paying | As today | As today |
| **New** | Warm-up only (below) | Warm-up only (below) |
| **Ended** | None | None |

Recollections come only from replies (`routes.py:241-242`), so New and Ended readers have none anyway.
Nothing in this section is ever shown to a reader.

**Ended: none, silently.** At the claim (`backend/app/hosted/jobs.py:400-416`), a companion job whose
reader is Ended is finished without work and without spending, as a cancelled job is. The routes stop
asking for new jobs for Ended readers (`routes.py:87-92`, `:157-160`). When they subscribe, the next
use of Mothli (`POST /api/reader/mothli/start`, or a question) asks for the book's chapter notes and
story memory again; both already build whatever is missing up to the reader's place (chapter notes
ten a run, `companion/jobs.py:476-481`; story memory one chapter a run, `:544-605`).

**New: the hard one.** The first session must be warm. The owner: "i dont want failures as soon as they
try it or not optimal answers just bcs we need some time to warm up". But a reader who never opens
Mothli should not cost $5 a novel in story memory forever.

**Recommended bound, the warm-up:** for a New reader, background work runs as for everyone, but only

- within **30 days of sign-up** (`users.created_at`), and
- up to **25 chapters of story memory that their reading is the first to build**, counted over the
  whole time they are New (the count `first_builds` already makes per day and month,
  `backend/app/hosted/jobs.py:745-766`, without the month limit), and
- up to **$0.50 of chapter notes** (their background ledger, `spend.py:78-94`), about 70 chapters.

Past any of the three, their background work stops silently, as for Ended. Their first use of Mothli
starts the free weeks and asks for it again.

**What it costs.** At most $0.50 + 25 × $0.09 = **$2.75 once per sign-up who never uses Mothli**, and only
if they import a file nobody else has. A New reader of a free-library book that others have read costs
only their chapter notes, at most $0.50. Together with the free ceiling, the worst a sign-up can cost is
about $2.10 + $2.75 + the background of their free weeks.

**What it does not cover.** A reader who reads for two months, or past chapter 25 of an imported novel,
before trying Mothli meets a cold start: the chat works at once (it answers from the pages they have
read, which never needs story memory), chapter notes catch up ten chapters a run, but the cast and
"Story so far" fill in one chapter every two or three minutes, and the panels show the existing
catching-up line meanwhile (`frontend/src/reader/StoryMemory.tsx:67`: "I’m still catching up on what
you’ve read. It’ll fill in here as I go."). The other choices are in Open point 6.

Where it hooks in: the claim (`hosted/jobs.py:400-416`) reads `reader_plan_state` for companion jobs;
the story memory run's guard (`companion/jobs.py:337-345`, `:552-556`) gains the warm-up count; chapter
notes are held by the claim. The worker role needs `SELECT` on `reader_plans` and `users.created_at`.

### 6. What each state sees, screen by screen

Every line below is written in Mothli's voice, as it would appear. None names a number, a counter or
"unlimited". "Mothli" speaks in the first person in its own panels; the account sheet speaks about it.

**The session.** `GET /api/auth/session` (`auth/api.py:226-243`) gains `mothli`: the state (`new`,
`free`, `ended`, `paying`), why it ended (`time`, `ceiling`, `plan`), whether to show the ending note,
and for the account sheet the plan's kind (monthly, yearly, gift) and its next date. No amount and no
count of questions is ever sent to the app.

**At sign-up** (`frontend/src/shelf/SignIn.tsx:124-172`). One added line under the welcome:
"Reading here is free, always. Talking with me is free for your first two weeks."

**New**

- Chat panel, above the box, until the first reply arrives:
  "Hi, I’m Mothli! Your first two weeks with me are free, so ask me anything about the pages you’ve
  read. No spoilers, promise."
- Cast and "Story so far": as today. Opening either starts the free weeks.
- The corner note after a chapter: as today ("That’s Chapter 3 done! Want to hear what I noticed?").

**Free weeks**

- Everything as today. No counter, no banner.
- **The gentle note, once, in the last two days**, at the top of the chat panel, with a small "Got it"
  that hides it (remembered on that device):
  "Just so you know, our free weeks together end on Thursday. Your books stay yours either way. If
  you’d like to keep talking after that, here’s how." with a button **"See the plan"**. The day is named
  in the reader's own time zone. No hours, no countdown.

**Ended**, in the chat panel (in a book and on the home screen), the cast and "Story so far": in place
of the panel's content, one invitation, with one button **"Keep talking with Mothli"** that opens the
plan sheet. Reading, the place, highlights, notes, bookmarks and the free library are untouched.

- Ended because two weeks passed:
  "Our two free weeks are over. I loved reading along with you! Your books, your place and your
  highlights stay right here, free, always. Join me on the plan and we’ll pick up right where we left
  off."
- Ended at the free ceiling, before day 14:
  "We’ve talked so much these first days that your free start with me is all used up. That’s a good
  sign! Your books, your place and your highlights stay right here, free, always. Join me on the plan
  and we can keep going right now."
- Ended after a paid plan ran out:
  "Your plan has ended, so I’m taking a little rest. Your books, your place and your highlights stay
  right here, free, always. Come back any time and we’ll pick up right where we left off."
- A question sent anyway (an old tab) gets the same invitation as the reply. A reply that had already
  begun finishes.
- The corner note after a chapter keeps its first part ("That’s Chapter 3 done!", and a milestone)
  and drops the offer. The names in the page are not looked up (the cast fetch answers "plan needed"
  and the page shows none, as it does today with no chat, `ReaderPage.tsx:1744-1748`).
- "What Mothli remembers" (`frontend/src/shelf/Account.tsx:200-213`) stays open to Ended readers:
  it is their own data, to read and to forget. Their conversations, chapter notes and what Mothli
  remembers are kept and come back with the plan. The copy of their data includes them as today.

**The plan sheet** (opened from any invitation, the ending note, or the account sheet):
"Keep talking with Mothli" as the title; two choices with prices read from Paddle in the reader's
currency (section 7): **Monthly** and **Yearly** ("that’s about … a month"); one line: "Cancel any time
from Your account. Your books stay free either way."; a note naming Paddle as the seller (Open point
10). Then Paddle's checkout.

**Paying**

- Nothing about limits, unless a guard is reached; then the day or month line of section 4.
- The account sheet (`Account.tsx:195-250`) gains a part:
  - "You’re on the monthly plan. It renews on 3 December." (or yearly)
  - **"Update payment details"** (opens Paddle's customer portal) and **"Cancel my plan"**.
  - Cancel asks once: "Cancel your plan? I’ll stay with you until 3 December, and you won’t be charged
    again. Your books stay yours, always." **"Cancel my plan"** / **"Keep my plan"**.
  - After cancelling: "Your plan ends on 3 December." with **"Keep my plan"**, which undoes it.
  - A payment Paddle is retrying: "Your last payment didn’t go through. Paddle will try again, or you
    can update your card here." with **"Update payment details"**. Mothli keeps working meanwhile.
  - A gift: "Mothli is on the house for you. Enjoy!" and no buttons.
- In the free weeks the account sheet also shows "Your free weeks end on 4 December." and **"See the
  plan"**. Subscribing then starts the plan that day (Open point 9).

Where it hooks in: `frontend/src/ui/companionPause.ts` gains the "plan needed" answer;
`frontend/src/reader/AskTheBook.tsx:632-653` (the chat's errors) and `:661-669` (the cast);
`frontend/src/reader/StoryMemory.tsx:224`, `:296`; `frontend/src/shelf/HomeChat.tsx:217-224`;
`ReaderPage.tsx:1634-1657` and `:1743-1755`; `Account.tsx:195-250`; `SignIn.tsx:124-172`. On the
server the gated routes answer `402` with `{"code": "mothli_plan", ...}` before anything else runs:
`routes.py:218-248` (ask in a book), `:338-385` (cast, a person's card, "Story so far", progress),
`:421-462` (the home chat), and the reply's own error event when the ceiling is met mid-way
(`backend/app/stream_conversation.py:744-752`, `backend/app/home_conversation.py:428-433`).

### 7. Paddle, end to end

Every fact below is from Paddle's pages as read on 20 November 2026.

**Accounts and keys.** Paddle has a separate **sandbox** with its own dashboard, keys and test cards;
keys from one do not work in the other ([sandbox](https://developer.paddle.com/sdks/sandbox)). Three
secrets per environment ([authentication](https://developer.paddle.com/api-reference/about/authentication);
[notification destinations](https://developer.paddle.com/webhooks/about/notification-destinations)):

- `PADDLE_API_KEY`: server only; "Treat your API key like a password". Needs permission to create
  transactions, read and cancel subscriptions, and create customer portal sessions.
- `PADDLE_WEBHOOK_SECRET`: the secret of the one notification destination; server only. Paddle shows it
  once.
- `PADDLE_CLIENT_TOKEN`: opens checkout from the browser; safe to show, still a setting.

Plus settings that are not secret: `PADDLE_ENVIRONMENT` (`sandbox` or `production`) and the two price
ids, `PADDLE_PRICE_MONTHLY` and `PADDLE_PRICE_YEARLY`. The prices themselves live in Paddle. The owner
pastes all of these into `deploy/app.env` himself; none is ever written in the repository or in this
record. The new database role adds `MOTHLI_DB_BILLING_PASSWORD` to `deploy/.env`.

**Subscribe.** "Keep talking with Mothli" → the plan sheet → the reader picks monthly or yearly →

1. The app asks the server, `POST /api/billing/checkout` (signed in, CSRF), for that plan.
2. The server creates a Paddle **transaction** with the price id and `custom_data`
   `{"mothli_reader": "<the reader's account id>"}`, with the reader's email as the customer, and
   returns only its id. Paddle copies a transaction's custom data to the subscription it creates and
   to every renewal ([custom data](https://developer.paddle.com/build/transactions/custom-data)).
   Creating it on the server, not in the browser, means a reader cannot attach their payment to
   another account's id. Paddle allows opening checkout with a `transactionId` "instead of an `items`
   array" ([Paddle.Checkout.open](https://developer.paddle.com/paddlejs/methods/paddle-checkout-open)).
3. The app opens Paddle's checkout over the page (overlay, Paddle's default) with that id.
4. The reader pays in Paddle's window. Paddle handles cards, tax and the receipt.
5. The checkout closing does **not** make anyone paying. Only the checked message does (next).
   Meanwhile the app shows "One moment, I’m getting ready…" and asks for the session again.

**The message that makes a reader paying.** Paddle sends signed messages ("webhooks") to one address,
`POST /api/billing/paddle`. The route:

1. Reads the raw body before anything parses it ("Don't transform or process the raw body"), and
   checks the `Paddle-Signature` header (`ts=…;h1=…`): HMAC-SHA256 over `ts:raw body` with
   `PADDLE_WEBHOOK_SECRET`, compared in constant time, accepting any `h1` during a secret rotation
   ([signature verification](https://developer.paddle.com/webhooks/signature-verification)). A message
   that fails is refused and nothing is written.
2. Refuses a `ts` too far from the server's clock. Paddle's own libraries default to five seconds;
   recommended here: five minutes, because the duplicate check below also stops a replay.
3. Records the `event_id` in `paddle_events`; one already seen is answered `200` and ignored. Paddle
   says the same message may arrive more than once
   ([respond to webhooks](https://developer.paddle.com/webhooks/respond-to-webhooks)).
4. Finds the reader: the stored `paddle_subscription_id`, or for a new subscription the
   `mothli_reader` in its custom data. An unknown or deleted account is recorded and ignored (the
   owner sees it in Paddle).
5. Applies the subscription's status and dates **only if the message is newer** than the last one
   applied (`occurred_at` against `paddle_occurred_at`): Paddle "can't guarantee the order of
   delivery" (same page).
6. Answers `200` within five seconds, which Paddle requires (same page). The work is one small
   transaction, so no queue is needed.

The messages used ([events](https://developer.paddle.com/webhooks/overview)):
`subscription.created` and `subscription.updated` carry everything (status, period, scheduled
cancel), as Paddle's provisioning guide recommends; `subscription.canceled`, `subscription.past_due`
and `subscription.activated` are applied the same way. `transaction.payment_failed` and
`adjustment.created` / `adjustment.updated` (refunds, chargebacks) are recorded only. Nothing else.

Paddle may also be limited to its published addresses: Paddle lists six for live and six for sandbox
(same page). Mothli sits behind Cloudflare, so the address the server sees is Cloudflare's; the check
would read Cloudflare's forwarded address. Recommended: rely on the signature, and add the address
check later if wanted.

**Renewal.** Paddle charges the card and sends `subscription.updated` with the new period. Nothing to
do but apply it.

**Cancel.** "Cancel my plan" → the server calls Paddle's cancel with `effective_from:
next_billing_period` ([cancel a subscription](https://developer.paddle.com/api-reference/subscriptions/cancel-subscription)).
The reader stays paying to the end of what they paid for; Paddle's message brings the date.
"Keep my plan" removes the scheduled cancel (`scheduled_change: null`,
[cancel subscriptions](https://developer.paddle.com/build/subscriptions/cancel-subscriptions)). Paddle
does not allow a cancel within 30 minutes of the next billing date (same page); the button then says
"Try again in a few minutes". A cancelled subscription cannot be brought back; subscribing again makes
a new one.

**Update payment details.** The server makes a **customer portal** session for the reader's customer
and subscription and opens its link in a new tab
([portal sessions](https://developer.paddle.com/api-reference/customer-portals/create-customer-portal-session)).
The link carries a temporary token and must not be kept or put in a frame (same page).

**Payment failure.** Status `past_due`, Paddle retries for up to 30 days and then cancels
([default payment recovery](https://developer.paddle.com/changelog/2025/default-dunning-payment-recovery)).
Mothli keeps working; the account sheet shows the line in section 6. In the sandbox "No payment retries
occur" (same page), so this path is tested with stand-in messages.

**Refund.** The owner refunds in Paddle's dashboard (or by API, an "adjustment") on a completed
transaction; most live refunds wait for Paddle's approval
([create adjustments](https://developer.paddle.com/build/transactions/create-transaction-adjustments)).
No Paddle page read says a refund ends the subscription. So: **a full refund is paired with "cancel
immediately"** in the dashboard, whose message makes the reader Ended. The app does not end a plan on
an adjustment message by itself.

**Taxes.** Paddle is the seller and collects and files sales tax and VAT
([pricing](https://www.paddle.com/pricing)). Each price is tax-inclusive, tax-exclusive, or by the
buyer's country ([create a price](https://developer.paddle.com/api-reference/prices/create-price);
[automatic inclusive and exclusive prices](https://developer.paddle.com/changelog/2025/automatic-tax-inclusive-exclusive-prices)).
Open point 3.

**Emails.** Paddle emails the buyer itself: receipts, subscription confirmation, scheduled-to-cancel,
cancelled, refund notices, and the renewal reminders the law requires for plans of six months or more
(the yearly plan), among others
([which emails customers receive](https://www.paddle.com/help/manage/your-customers/which-emails-will-customers-receive-on-paddle-billing)).
Mothli sends no email of its own and needs no mail service for this.

**Showing prices.** The plan sheet reads the two prices from Paddle with the client token, in the
reader's currency, so no price is written in the app.

**The browser's rules for outside scripts.** The app allows scripts and frames only from itself
(`backend/app/main.py:133-147`, `deploy/Caddyfile:25`, `frontend/vite.config.ts:6-14`). Paddle's
checkout needs its script and its window allowed. Paddle's own list of addresses was not found; the
step that adds checkout finds it in the sandbox (could not verify, 4).

**Orders under $10.** Paddle's pricing page says the fee is "5% + 50¢" a transaction, and its FAQ
answers "Do you charge a different fee for lower order values?" with: "If you’re selling product with
under 10$ value you can contact us for bespoke pricing" ([pricing](https://www.paddle.com/pricing)).
In plain words:

- $9.99 is under $10, so the monthly plan is in the range where Paddle invites a conversation.
- The page does **not** say small orders are refused or need special terms. It is an offer to
  negotiate, not a requirement. The pricing report's "need bespoke pricing" says more than the page
  does.
- On standard terms the fee on $9.99 is about $0.50 + $0.50 = **about $1.00, about 10%**. On $79.99 it
  is about $4.00 + $0.50 = **$4.50, about 5.6%**. (This arithmetic is mine, not Paddle's.)
- Whether the $10 is counted before or after tax is not said. With tax added on top, a US buyer's
  total can pass $10 while the price is $9.99.
- So: the owner writes to Paddle before launch and asks for their rate on a $9.99 monthly plan. If
  the answer is the standard rate, $7.33 a month after tax and fee (section 4) still holds. Open
  point 2.

**Approval of the site.** Paddle reviews each domain that opens a checkout. It wants a product
description, a pricing page or pricing details, Terms and Conditions, a Refund Policy and a Privacy
Policy "clearly accessible via navigation", the seller's legal name in the terms, and a live HTTPS
site ([domain approval](https://www.paddle.com/help/start/account-verification/what-is-domain-approval);
[why a domain is rejected](https://www.paddle.com/help/start/account-verification/why-has-my-domain-been-rejected)).
Mothli has none of these pages today. Paddle's acceptable use policy does not name AI chat as
forbidden; it forbids deepfakes and voice impersonation among other things, and says its list "is not
an exhaustive list" ([acceptable use](https://paddle.com/support/aup/)). Whether an AI reading companion
passes review is not known until Paddle reviews it.

**Refunds, in the buyer's terms.** Paddle's refund policy for buyers says transactions are
non-refundable except where the law says otherwise, Paddle may refund at its discretion within 14 days,
and buyers in the EU, EEA, UK and some other countries have a 14-day right to withdraw that is lost once
they have consented to start using digital content
([refund policy](https://www.paddle.com/legal/refund-policy)). Mothli's own refund page is the owner's
choice (Open point 8).

**Deleting an account while paying.** Asking to delete the account (`backend/app/hosted/auth/account.py:87-102`)
also schedules the cancel at the period's end, so the reader is never charged again. Changing their mind
within the 30 days offers "Keep my plan". Erasure removes Mothli's copy of the plan; Paddle keeps its own
records as the seller, which the privacy page says.

### 8. Gifts: the owner's account and test accounts

The owner's own account and test accounts are **Paying without Paddle**: `reader_plans.gift_until`.

- Set only by the owner, on the server: `python -m app.hosted.plans gift <account id> [--until
  2027-06-30]`, and `... gift <account id> --end`, run in the operator container (as `python -m
  app.hosted.limits` is, `backend/app/hosted/limits.py:110-176`). It calls one function,
  `set_gift(owner, until)`, that only the operator role may execute.
- No date means "for good" (a far date, so the column stays a date).
- `... show <account id>` prints the state, the dates and the spend since the free weeks began, and
  nothing else (no book, no words).
- A gift is never visible to other readers. The account sheet says "Mothli is on the house for you."
- Gifted readers have the paying readers' hidden guards. The owner can lift his own with the existing
  `owner_limits` override (`limits.py`).

### 9. Spoiler protection: nothing here removes or weakens a layer

**Nothing in this record removes, bypasses or weakens any layer of spoiler protection.** For every book,
Mothli knows nothing beyond that book's read position, in every answer, including answers across books.
A plan never changes what Mothli reads, searches or sends, for any reader.

Why, layer by layer:

| Layer | Where | What the plan does to it |
|---|---|---|
| The read map: which pages were read, and the place | `app.read_map`, `routes.py:111-216` | Nothing. Page reports are taken in every state, paying or not, so the place is always right when Mothli is used |
| The search over read text only | `stream_read_prefix` (`routes.py:239-242`) | Nothing. Called with the same arguments, or not called |
| The door to story memory, up to the reader's place | `story_memory_door`, migrations 0026 and 0032 | Nothing. Not read at all for Ended; read as today otherwise |
| The check of every sentence (Jev) | `app.stream_conversation` | Nothing. Same calls; the plan check never admits an unchecked sentence |
| The later-name check | migration 0035 | Nothing |
| Stored conversations shown only while their pages are still read | `visible_turns`, `routes.py:251-273` | Nothing. Kept for Ended readers, shown through the same filter when they return |
| The home chat's place in each book | `stream_home`, `routes.py:421-439` | Nothing. Gated in front, unchanged behind |
| Story memory built one chapter past the place | `companion/jobs.py:322-334` | Nothing. The worker only decides whether a run happens; when it does, the target is computed from the read map as today |

The plan check **sits in front of the conversation and never changes its inputs**. In the routes it is
the first thing that runs, before the reader's catalog, source and story memory are even opened; it
answers yes (the route goes on exactly as today) or no (the route answers "plan needed" and nothing
else runs). In the spend check it only admits or refuses a model call, as the day and month caps already
do (`spend.py:373-381`); a refused call is refused before any text is chosen or sent. In the worker it
only finishes a job without work or lets it run. No plan state is passed to any function that chooses
text, builds a prompt, searches, or reads story memory. A reader who pays sees what a reader who does
not pay would have seen while in their free weeks, no more and no less.

The tests of step 5 prove it: the same question at the same place gives the same retrieval and the same
prompt for a reader in the free weeks, a paying reader and a gifted reader (stand-in models), and an
Ended reader's request reaches no model, no search and no story memory read.

### 10. Abuse and limits, honestly

**A new account gets a new taste.** Anyone can sign in with a new email through the sign-in service and
get another two weeks and another $2.10. Deleting an account and signing up again with the same email
also works, once the old account is erased (30 days), because erasure removes the row that remembers
the free weeks.

What would reduce it, and what is built now:

- **Built now:** the ceiling is in dollars and per account, so each new account costs at most about
  $2.10 in chat plus the warm-up's bound. A day budget for all readers in their free weeks (Open
  point 4) caps a sudden wave of new accounts without touching paying readers.
- **Not built now:** a verified email before the free weeks start (the sign-in service reports it,
  `external_identities.email_verified`, `0001_identity_and_accounts.sql:21-35`); keeping a one-way
  fingerprint of an erased account's email to refuse a second taste (at odds with "erasure removes
  everything", so it needs the owner's decision and a privacy-page sentence); limiting new accounts per
  network (the sessions keep a fingerprint of the address range, `0001:46`); a card at the start of the
  free weeks (Paddle trials). Each is a later, separate decision, only if abuse is seen.

### 11. Steps

Small pull requests, each green on its own (`ruff check app tests scripts`, the hosted Postgres suite
locally), stand-in models only, tests in the same pull request written before the code they test.
Forward-only migrations from **0037**. Paddle is only ever the sandbox in tests and never called by the
test suite (its messages are made and signed in the tests).

1. **The free weeks, and no more stop at the sixth question.** Migration
   `0037_reader_plans.sql`: `reader_plans` (row-level security forced, tenant `SELECT`),
   `start_free_weeks()` and `reader_plan_state(...)`. Settings `HOSTED_FREE_DAYS` (14) and
   `HOSTED_FREE_USD` (2.10). The ask routes (`routes.py:218-248`, `:421-439`) start the free weeks;
   `reserve_cost` (`spend.py:347-381`) skips the day and month caps in the free weeks and applies the
   free ceiling instead. Tests first: a reader in the free weeks is not stopped by today's $0.10 day; the
   ceiling stops the next reply and lets the one under way finish; the clock is stamped once and not by a
   second question, a removed book or a cleared conversation; the tenant role cannot write the row or
   another reader's; the function takes no date. **Reader sees:** their first two weeks of talking are
   never cut off at the sixth question. At the free ceiling, until step 5, the ceiling line of section 6
   without its button; after the two weeks, today's day and month caps, as now.
2. **Gifts.** `app.hosted.plans` (gift, end, show) and `set_gift`, executable by the operator role
   only; gifted readers are Paying in `reader_plan_state`. Tests: the command sets and ends a gift; the
   tenant role cannot call `set_gift`; a gifted reader has the paying guards. **Reader sees:** nothing;
   the owner and testers are never stopped by the free ceiling.
3. **Paddle's messages.** Migration `0038_paddle_events.sql`: `paddle_events` (event id, type, time,
   account, outcome; no payload kept); the `billing` role in `hosted_roles.py` and `grants.py`;
   `POST /api/billing/paddle` (signature, time window, duplicates, order, matching). Settings
   `PADDLE_ENVIRONMENT` and `PADDLE_WEBHOOK_SECRET`. Tests first, with messages signed in the test: a bad
   or missing signature writes nothing; an old timestamp is refused; a repeat is ignored; an older message
   after a newer one changes nothing; `active`, `past_due`, scheduled cancel, `canceled`, `paused` give the
   states of section 1; an unknown account is recorded and ignored; the billing role cannot read any other
   table. **Reader sees:** nothing yet.
4. **Subscribe, cancel, payment details.** `POST /api/billing/checkout`, `/cancel`, `/keep`, `/portal`
   (signed in, CSRF), through the billing role and Paddle's API with `PADDLE_API_KEY`; the plan sheet and
   the account sheet's part (section 6); Paddle's script and window allowed in the three security
   policies; `deploy/app.env.example` gains the Paddle lines as placeholders. Tests: the server, never the
   app, chooses the account in the custom data; the app shows prices it was given and none of its own;
   cancel schedules at the period's end; frontend tests for every line of the account sheet. Then a walk
   through the sandbox by hand with Paddle's test cards, written up in the pull request. **Reader sees:**
   they can subscribe and cancel from Your account.
5. **The gate and the words.** The session's `mothli`; `402 mothli_plan` on the routes of section 6; the
   invitations, the hello, the ending note and the sign-up line; `POST /api/reader/mothli/start` from the
   cast and "Story so far"; the corner note without its offer for Ended readers. Tests first: the
   spoiler tests of section 9; each state's answer on each route; the automatic cast and notes fetches
   never start the clock; every line of section 6 in a frontend test, and none contains a digit of an
   allowance, "unlimited", "questions left" or a countdown. **Reader sees:** section 6, all of it.
6. **Background work by state.** The claim (`hosted/jobs.py:400-416`) finishes an Ended reader's
   companion jobs without work and holds a New reader past the warm-up; the story memory guard
   (`companion/jobs.py:337-345`) gains the warm-up count; the routes stop asking for jobs for Ended
   readers; the start call and a paying reader's first question ask for them again. Settings
   `HOSTED_WARMUP_DAYS` (30), `HOSTED_WARMUP_NEW_CHAPTERS` (25), `HOSTED_WARMUP_NOTES_USD` (0.50). Tests:
   an Ended reader's jobs spend nothing; a New reader is built for within the bound and not past it;
   subscribing resumes both kinds up to the place and not past it. **Reader sees:** nothing (by design).
7. **Deleting an account while paying.** The deletion request schedules the cancel; "Keep my plan" on
   the waiting screen (`SignIn.tsx:228-268`) when they change their mind; the export's "your plan.json".
   Tests with Paddle's API stood in. **Reader sees:** "Your plan won’t renew." on the waiting screen.
8. **Words and pages.** Terms, Refund Policy and Privacy pages reachable from the app's navigation and the
   sign-in page (Paddle's review); the runbook (`docs/runbooks/hosted-deployment.md`): Paddle's
   notification address, the secrets, the billing password, the gift command, the sandbox walk; ADR 0041
   section 6 marked as amended by this record; `docs/product/companion-open-items.md` updated.
   **Reader sees:** the three pages.

Steps 1 and 2 stand alone and can ship before Paddle. Step 5 must not ship before step 4, or Ended
readers would see a button that leads nowhere.

## Where this record disagrees with the planner's design

1. **"The first time they open the cast or Story so far" cannot be seen by the server alone.** The app
   fetches both by itself (`ReaderPage.tsx:1634-1657`, `:1743-1755`). Kept the design; added one call
   from the app (section 2).
2. **The free weeks have no day stop, but the platform's day still stops everyone.** At $15 a day, about
   seven new readers talking to their ceiling on one day would rest every reader, paying ones included,
   until the next UTC day. Kept "no day stop for a reader"; added Open point 4.
3. **Subscribing during the free weeks starts the plan that day.** Paddle bills from the checkout; keeping
   the free days on top would need a Paddle trial on the price, which this record did not verify works
   without a card. Open point 9.
4. **The month ceiling uses the UTC calendar month, not the reader's billing month**, because that is how
   the spend check already counts (`spend.py:64`, `:88-94`). A reader who subscribes on the 28th can meet
   a fresh month three days later. Simpler and kinder; noted, not changed.
5. **The pricing report's "orders under $10 need bespoke pricing" is stronger than Paddle's page**, which
   only invites a conversation (section 7).

## Open points for the owner

Each is a question with choices; the recommendation is first.

1. **Prices.** (a) $9.99 a month, $79.99 a year (33% off, "four months free"), as proposed. (b) $9.99 and
   $89.99 (25% off; more left from a heavy yearly reader). (c) $10.99 and $89.99 (clearly over $10 at
   Paddle). Recommended: (a), after the answer to point 2.
2. **Paddle and the $9.99 price.** (a) Write to Paddle before launch, ask their rate for a $9.99 monthly
   plan, and launch on whatever they answer. (b) Launch on the standard 5% + $0.50 without asking.
   Recommended: (a); it costs one email.
3. **Tax on the price.** (a) By the buyer's country: tax included in Europe and the UK, added on top in
   the US. (b) Always included. (c) Always added. Recommended: (a), if Paddle's account offers it; EU and
   UK consumers expect prices with tax included.
4. **The platform's day, now that the free weeks have no day stop.** (a) Raise `HOSTED_PLATFORM_USD_PER_DAY`
   to fit, and add a day budget for all free-weeks readers together (proposed $20), past which free-weeks
   readers see today's platform line ("I’m resting for the rest of today and I’ll be back tomorrow. Your
   book and your place are right here.") and paying readers never stop. (b) Raise the platform's day only.
   (c) Leave it at $15. Recommended: (a); the numbers to be set from the first weeks' sign-ups.
5. **Story memory for paying readers who import many new books.** (a) Leave ADR 0044's guards as they are
   (up to about $27 a month for one reader's first builds). (b) Lower the month guard for new chapters from
   300 to 120 (about $11). Recommended: (a) until real use shows a problem; the guards are settings.
6. **New readers' warm-up.** (a) 30 days, 25 new chapters, $0.50 of notes (about $2.75 at most, once).
   (b) Build for New readers without a bound (warmest; up to about $5 a novel nobody else has, for
   readers who may never try Mothli). (c) No warm-up (cheapest; every first session starts cold).
   Recommended: (a).
7. **The ceilings in numbers.** Free: $2.10 (about 100 replies). Paying: $0.60 a day and $4.00 a month
   (section 4's table; $3.00 is safer for the yearly plan, $5.00 loses money on a heavy yearly reader).
   Recommended: $2.10, $0.60, $4.00.
8. **Mothli's own refund policy** (Paddle needs a page). (a) "Ask within 14 days of a payment and we’ll
   refund it, no questions asked." (b) Paddle's buyer terms only (refunds where the law requires them).
   Recommended: (a); simple, kind and cheap at these prices.
9. **Subscribing during the free weeks.** (a) The plan starts that day; the account sheet says so before
   checkout. (b) Try to keep the remaining free days with a Paddle trial (needs checking in the sandbox).
   Recommended: (a).
10. **Naming Paddle on the plan sheet.** (a) One plain line: "Payments are handled by Paddle, who sells
    Mothli on our behalf." (b) Only what Paddle's checkout itself shows. Recommended: (a); it explains the
    name on the reader's card statement. Whether Paddle requires it was not verified.
11. **The gentle note.** (a) Once, in the last two days, naming the day. (b) Also a single line on the
    first day ("Your two free weeks run until 4 December"). (c) No note at all. Recommended: (a).
12. **Abuse.** (a) Nothing more now; watch sign-ups. (b) Require a verified email before the free weeks
    start. Recommended: (a), with (b) ready if a wave appears.

## Consequences

- Readers read free forever; Mothli is one plan, and the first step ends today's stop at the sixth
  question.
- A sign-up costs at most about $2.10 in chat, plus a bounded warm-up; a reader who never pays costs
  nothing after their free weeks.
- A new database table and role, two migrations, one Paddle message route, four small billing routes, one
  operator command, and three public pages.
- The owner holds three new secrets and two price ids. No secret is in the repository.
- No layer of spoiler protection changes.

## What this record could not verify

1. Whether Paddle's rate for a $9.99 plan differs from 5% + $0.50, and whether its $10 is counted before or
   after tax (only Paddle can say).
2. That a refund does not end the subscription by itself: no Paddle page read says either way.
3. Whether Paddle's emails to buyers can be turned off or reworded; nothing here depends on it.
4. The exact addresses Paddle's script and checkout window need in the browser's security policy.
5. The shape of `custom_data` in a real message (an older Paddle page describes it as text holding JSON);
   to be seen in the sandbox in step 3.
6. How long a customer portal link lasts (Paddle says the token is temporary, no figure).
7. Whether Paddle approves an AI reading companion.
8. Whether Paddle's checkout records the EU buyer's consent to start using digital content at once, which
   decides the 14-day withdrawal right; legal advice before launch is worth it.
9. That old `story_memory` job rows are kept long enough to count a New reader's first builds over 30 days
   (`first_builds` reads them; their removal was not traced).
10. Every money figure: 2.1 cents a reply, 0.7 cents a chapter of notes and 7 to 9 cents a chapter of story
    memory come from earlier measurements and records, not from a run for this record.
