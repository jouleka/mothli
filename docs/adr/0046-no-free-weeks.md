# ADR 0046 — No free weeks: talking with Mothli is the plan from the first question

**Status:** Accepted (2026-11-27, the owner's decision). Amends ADR 0045: its decisions 2, 3, 4
(the free ceiling), 6 (the hello and the ending note) and 10, its sections 1, 2, 3 and 6 where they
speak of the free weeks, and steps 1, 2, 5 and 6 as built. Everything else in ADR 0045 stands.

## Decision

The two free weeks are removed completely. Reading stays free, always. Talking with Mothli (the
chat in a book, the home chat, the cast, a person's card, "Story so far" and what Mothli has said)
needs the plan from the first question: a gift from the owner, or a live Paddle plan.

## What replaced each part

- **The states.** A reader is `new` (has never had a plan or a gift), `paying` (a gift that lasts
  today, or a live Paddle plan, exactly as before) or `ended` (had a plan or a gift and it stopped;
  why `plan`). `free`, and the whys `time` and `ceiling`, are gone. A reader who was in their free
  weeks, or past them, and never had a plan or a gift, is `new`. A gift ended with the command's
  `--end` leaves no date behind, so that account is `new` again unless it has had a Paddle plan;
  a gift that runs out keeps its date and the account is `ended`. Either way Mothli is closed to
  it until it pays.
- **The gate.** Every gated route refuses a `new` reader exactly as it refused an `ended` one:
  `402` with `{"code": "mothli_plan", "why": null}` (`"plan"` for an ended reader), from the route's
  dependency, before anything is read, searched or reserved. `reserve_cost` refuses both with
  `PlanNeeded` in the stream, for a reader who stops paying between the gate and the reply. A
  reader's own `owner_limits` caps never let a reader who does not pay through.
- **Nothing starts free weeks.** `start_free_weeks()`, the stamp at the first question and
  `POST /api/reader/mothli/start` are removed (the endpoint inventory is version 27).
- **The database.** Migration `0042_no_free_weeks.sql` drops `start_free_weeks()` and
  `reader_plans.free_started_at`, and makes `reader_plan_state(reader)` again with one argument
  and without the free weeks' columns (`free_started_at`, `free_ends_at`, `free_spent_usd`). The
  first paid month (`first_paid_at`, `first_month_spent_usd`) and gifts are unchanged. The role
  lists (`grants.py`, `scripts/hosted_roles.py`) name the new signature.
- **Settings.** `HOSTED_FREE_DAYS` and `HOSTED_FREE_USD` are removed; an env file that still names
  them is ignored, as every unknown name is. `python -m app.hosted.plans show` prints the state,
  why, who pays and the gift's date, and nothing of free weeks.
- **The screens.** A `new` reader sees an invitation in place of each Mothli panel, worded for
  someone who has never had Mothli, whose button ("Talk with Mothli") opens the plan sheet; an
  ended reader keeps the ended invitation and "Keep talking with Mothli". The hello, the ending
  note, the sign-up line and the front page's free-weeks line are gone, and the plan and account
  sheets say nothing of free weeks.
- **The public pages.** The terms say the plan is paid every month or every year, with the prices
  on the front page and in the checkout. The refunds page loses its "Try it first" section and
  nothing else. The privacy page and the copy of one's data no longer name when the free weeks
  began, since that is no longer kept.

## What is kept

- **The warm-up**, exactly as bounded in ADR 0045 section 5: a `new` reader's background work runs
  within 30 days of sign-up, up to 25 chapters of story memory their reading is the first to
  build and $0.50 of chapter notes, counted over the whole time they are new; then silently none.
  So a reader who joins finds Mothli warm. `paying`: as today. `ended`: none.
- Spoiler safety and the checking of every shown sentence: nothing here changes what is read,
  searched, sent or checked.

## Keep their question (2026-11-27)

A reader who does not pay keeps the box in the chat in a book and on the home screen. A question
they ask (typed, a shortcut, or "Remind me who ... is." from a name) is not sent: it stays on
screen as theirs, with an invitation under it that names the book and the chapter they are in
(`keptInvitation` in `frontend/src/ui/MothliPlan.tsx`), never a price or a number of replies, and
one button for the plan sheet. Closing the sheet leaves it with "I'll keep this for you."; the
reader can edit or remove it. It is kept per book (and once for home) on this device only
(`frontend/src/ui/keptQuestion.ts`, localStorage), never on the server. When where the reader
stands turns `paying`, the panel takes the question out of keeping and sends it in the same step,
so it is sent once, as if just asked; a `402 mothli_plan` on it keeps it again. The cast and
"Story so far" keep the plain invitation. The server is unchanged: the gate still refuses before
anything is read or reserved.
