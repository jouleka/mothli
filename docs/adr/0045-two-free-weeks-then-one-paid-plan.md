# ADR 0045 — Two free weeks, then one paid plan: reading stays free, Mothli is the plan

**Status:** Proposed (2026-11-20). Steps 1, 2, 3, 4 (server and screens), 5 (server and
screens), 6 and 7 (deleting an account while paying, and "your plan.json"), and Mothli's own
payment link in the shared Paddle account ("One Paddle account, several products", at the end)
are built (see below), short of the sandbox walk; nothing else is (step 8, the pages, is not). The
owner's decisions in "What the owner decided" are settled; the numbers and the choices in "Open
points for the owner" are his to make, and point 7 is decided (23 November 2026).

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

**Step 2 built (2026-11-23).** Migration `0038_reader_gifts.sql`: `reader_plans.gift_until` (a
`DATE`, NULL for none, `9999-12-31` for "for good"; a gift lasts to the end of its UTC day);
`set_gift(reader, until)`, a security definer that only the operator role may execute (it makes the
reader's row if they have none, and with NULL ends the gift; it answers whether the account
exists); and `reader_plan_state` made again with three more columns, `paid_by` (`gift` while a
gift lasts; Paddle's plans come later), `gift_until`, and `free_spent_usd` (the reader's own chat
since their free weeks began, for the operator's look). A live gift answers `paying`; a passed one
leaves the reader `new`, `free` or `ended` as they would be. The operator role gains `SELECT` on
`reader_plans` and `EXECUTE` on the two functions (`backend/scripts/hosted_roles.py`), and the
deployment's check now verifies its functions as it does the tenant's. The command
`python -m app.hosted.plans` (`gift ID [--until YYYY-MM-DD]`, `gift ID --end`, `show ID`) runs in
the `tools` container; `show` prints the state, the dates and that spend as one line of JSON,
nothing else. The spend check needed no new code: `paying` is not `free`, so a gifted reader is held
by the reader's day and month caps with today's lines, or by their own `owner_limits` caps where set,
and never by the free ceiling. A gifted reader's first question still stamps their free weeks, once,
so they cannot have them after the gift ends. Choices made in building it:

- **`set_gift` takes a date, not a time**, as section 8 says ("a far date"). `--until 2027-06-30`
  is Mothli on the house through the end of that UTC day.
- **`show` reads `HOSTED_FREE_DAYS` and `HOSTED_FREE_USD` from its own environment** (the app's
  defaults when they are absent), because the `tools` container is given no `deploy/app.env`. If
  the owner ever changes either in `app.env`, he passes the same with `-e` (the runbook says so).
  It affects only the state `show` prints for a reader without a gift.
- **The Paddle messages' migration moves to the next free number** (`0039_paddle_events.sql` in
  step 3), since 0038 is the gifts'.
- **A paying reader's hidden guards are the owner's numbers**, decided with this step (section 4):
  `HOSTED_READER_USD_PER_DAY` 1.00 and `HOSTED_READER_USD_PER_MONTH` 4.00 by default. Without this,
  a gifted reader, the owner's own account included, would rest at about the sixth question a day.

**Step 3 built (2026-11-23).** Prices decided by the owner: $12.99 a month and $79.99 a year (a
launch price; later $89.99 as a new Paddle price, readers on the old one keeping it). Migration
`0039_paddle_events.sql`: on `reader_plans` the columns of section 1 (`paddle_customer_id`,
`paddle_subscription_id`, unique, `paddle_status`, `period_ends_at`, `ends_at`,
`paddle_occurred_at`) and `first_paid_at`; `paddle_events` (`event_id`, `event_type`,
`occurred_at`, `owner_id` or NULL, `outcome`, `received_at`; no payload, no customer, no amount;
row-level security forced; the only table whose `owner_id` may be NULL, and the purge takes an
erased account's rows with the rest); `paddle_reader(subscription, named)`, a security definer only
the billing role may execute, answering the account that holds the subscription, else the named
account if it exists and is not erased, else NULL; and `reader_plan_state` made again: a gift that
lasts wins, then a live Paddle plan (`active`, `trialing`, `past_due`, and `active` with a
scheduled cancel or pause until its date) is `paying` with `paid_by = 'paddle'`; any other Paddle
plan (`paused`, `canceled`, past its scheduled end) is `ended` with why `plan`, before the free
weeks are looked at, so a reader who paid and stopped is never back in them. New columns:
`paddle_status`, `period_ends_at`, `plan_ends_at`, `first_paid_at`, `first_month_spent_usd`.

The `billing` role (`backend/scripts/hosted_roles.py`, lists in `grants.py`, password
`MOTHLI_DB_BILLING_PASSWORD`, address `HOSTED_BILLING_DSN` given to the web container only):
`SELECT, INSERT, UPDATE` on `reader_plans`, `SELECT, INSERT` on `paddle_events`, as section 1
says, and `EXECUTE` on `paddle_reader`, which section 1 does not list. It does not bypass row-level
security. Why the one function: under row-level security the role sees only the row of the account
its transaction is set to, so it cannot itself find which account holds a subscription id; the
function answers that one id and nothing else, which is far narrower than bypassing row-level
security. The deployment's check verifies the role's table rights and its function both ways.

`POST /api/billing/paddle` (`backend/app/hosted/billing.py`), registered only when
`PADDLE_WEBHOOK_SECRET` is set; no session and no CSRF. It reads the raw body (at most 256 KiB),
checks `Paddle-Signature` as section 7 says (any `h1`, constant time) and `ts` within
`PADDLE_WEBHOOK_TOLERANCE_SECONDS` (300), then applies the message in one transaction on the
billing role, set to the account, and answers 200. Settings: `PADDLE_ENVIRONMENT`
(`sandbox`/`production`; read by nothing yet, for step 4's API calls), `PADDLE_WEBHOOK_SECRET`
(must start `pdl_ntfset_`), `PADDLE_PRICE_MONTHLY` and `PADDLE_PRICE_YEARLY` (each one id or
several, comma-separated, so an old price stays accepted), `PADDLE_WEBHOOK_TOLERANCE_SECONDS`,
`HOSTED_BILLING_DSN`, `HOSTED_FIRST_PAID_MONTH_USD`. Choices made in building it:

- **The shared Paddle account.** The owner's Paddle account also sells MySigner, whose messages
  reach the same address. A message is Mothli's only when its `event_type` is `subscription.*` and
  every item's `price.id` is one of Mothli's price ids from the settings; anything else is
  answered 200 and kept nowhere (no row, no id, no log line). Custom data on Paddle's product
  (`app=mothli`) and prices (`plan=...`) is not read: it can be edited in the dashboard or copied
  onto another product, the price ids cannot be mistaken. The reader named in a subscription's
  custom data (`mothli_reader`) is looked at only after the price check passes.
- **Which account.** The account holding the subscription id wins; the custom data names the
  account only for a subscription Mothli has not stored. A message naming no account, an unknown
  one or an erased one is recorded with no account (`no_account`) and ignored.
- **A second subscription for one reader.** Another subscription naming a reader takes the place
  of theirs only once theirs is no longer live (cancelled or paused, or past its scheduled end):
  a new plan after a cancel. While their own plan is live, any other subscription naming them,
  live or not, is recorded `other_subscription` and changes nothing, so someone who subscribed in
  their name and then cancelled cannot end their plan. The cost: a reader whose plan is cancelled
  at once (a refund) and who subscribes again before Paddle's cancel message arrives has the new
  subscription recorded `other_subscription`, and shows as ended when the cancel arrives, until
  the new plan's next message (its renewal). Step 4's reconciliation repairs it sooner.
- **Only subscription messages.** Section 7 recorded `transaction.payment_failed` and
  `adjustment.*`; they are not read at all (a failed payment arrives as `subscription.past_due`
  anyway, and adjustments carry no price id to tell Mothli's from MySigner's). The runbook has the
  destination subscribe to the `subscription.*` events only.
- **The logs** say only that a message was refused (signature, or a Mothli message that cannot be
  read); what was done with each message is kept in `paddle_events`.
- **The first paid month** (section 4), its own commit. Rule: for the 30 days after
  `first_paid_at` (the first time a Paddle plan applied as `active` or `past_due`, from the database
  clock, kept once per account and never moved), while a Paddle plan pays and no gift does, the
  reader's month check is their chat since `first_paid_at` against `HOSTED_FIRST_PAID_MONTH_USD`
  ($6.30) instead of their calendar month against $4.00. The day guard stays; an operator's own
  month cap wins. **What the reader sees at its ceiling** is always true: they can talk again
  when the 30 days end, unless those end within this UTC calendar month and this month's chat
  has already reached the ordinary month, in which case they can on the 1st and see today's
  month line ("…I’ll be back at the start of the next…"). Otherwise they see a line of its own
  naming the day: "We’ve talked our way through your first month together already! I’ll be back
  on 22 December. Your book and your place are right here." The day is the first that has begun
  nowhere on Earth before the 30 days end (the day after their end in UTC+14, or that day when
  they end at its midnight), since the server does not know the reader's time zone; so in UTC it
  can be a day after the real moment. No amount, no count, no countdown. **The bound**: within the 30
  days a reader's chat reaches at most $6.30, however the days fall across calendar months (plus,
  as for every cap, the replies already under way). After them the calendar month counts everything
  spent in it, first month included, so for a reader whose 30 days straddle two calendar months the
  30 days and the rest of the second month together reach at most $6.30 + $4.00 = **$10.30**, and
  no calendar month more than **$6.30**. (Swapping the month's ceiling for $6.30 for 30 days, the
  simpler-looking rule, would allow $12.60 in those 30 days: $6.30 in each calendar month.) What
  a reader spent before paying (their free weeks) does not count against it. A reader who spends
  the $6.30 in the second calendar month rests until the 1st after the 30 days end.

Where Paddle's documentation (read 23 November 2026) differs from this record, or adds to it:

- **Respond first, then work.** [Respond to webhooks](https://developer.paddle.com/webhooks/respond-to-webhooks)
  says to answer within five seconds and to "Respond before doing any internal processing", with a
  queue. Not followed: the work is one transaction of a few statements (connection timeout three
  seconds), and doing it before answering means a failure answers 500, so Paddle delivers again,
  where a queue would need its own retry. If deliveries ever time out, a queue is the change.
- **Retries.** The same page: live, up to 60 attempts over about two days; sandbox, 3 within about
  two minutes. Nothing here depended on it.
- **Custom data is a JSON object**, not text holding JSON ([custom data](https://developer.paddle.com/build/transactions/custom-data);
  the `custom_data` field of the [subscription event](https://developer.paddle.com/webhooks/subscriptions/subscription-updated)).
  "Could not verify" 5 is answered by the docs; still to be seen in the sandbox.
- **Custom data can be set from the browser**: Paddle.js takes `customData` in
  `Paddle.Checkout.open()` (same page). Section 7's server-made transaction keeps Mothli's own
  checkout honest, but anyone with the public client token and a Mothli price id could open a
  checkout naming any reader. They would pay for that reader's plan; no reader gains access without
  someone paying for that price. Step 4 may want a server token in the custom data to refuse even
  that.
- **Statuses and scheduled changes.** The [subscription event](https://developer.paddle.com/webhooks/subscriptions/subscription-updated)
  lists `scheduled_change.action` as `cancel`, `pause` or `resume`; a scheduled cancel or pause sets
  `ends_at`, a resume does not. `current_billing_period` is null for paused and cancelled plans.
  [Provision access](https://developer.paddle.com/build/subscriptions/provision-access-webhooks):
  `trialing` "Treat the same as `active`", `past_due` keeps access, `paused` none, `canceled` none
  unless a scheduled change is pending: as section 1. The same page recommends a periodic
  reconciliation against Paddle's API; not built (it needs the API key, step 4).
- **More subscription events** than section 7 lists ([webhooks overview](https://developer.paddle.com/webhooks/overview)):
  also `subscription.imported`, `.paused`, `.resumed`, `.trialing`. Each carries the whole
  subscription and is applied the same way.
- **Signature.** [Signature verification](https://developer.paddle.com/webhooks/signature-verification):
  the secret key starts `pdl_ntfset_` and is used as it is; more than one `h1` may come "while old
  secrets are phased out", and the page says rotation support may be added in future. The SDKs'
  five seconds are the default tolerance; kept at five minutes here, as section 7 recommends.
- **The dashboard.** Destinations are made under **Events > Notifications**, choosing the events and
  whether simulated events are delivered ([notification destinations](https://developer.paddle.com/webhooks/about/notification-destinations)).

Not verified without a Paddle sandbox: that a retried delivery carries a fresh `ts` (with five
seconds as the SDKs' default it must, but no page says so; a stale `ts` on a retry would be refused
and retried until it fails); the exact payload of a real message; that the simulator's sample is
for a price that is not Mothli's (the runbook's step 4 assumes so).

**Step 4, server half, built (2026-11-23).** The routes and the regular check; no screen changes
(the plan sheet and the account sheet's part come next). Nothing here was run against Paddle: no
sandbox account exists yet, and every Paddle call in the tests goes to a stand-in behind the real
client.

- **Migration `0040_paddle_checkouts.sql`.** `paddle_checkouts` (`owner_id`, `token_digest`,
  `created_at`, `expires_at`, `used_at`; key `(owner_id, token_digest)`; row-level security
  forced): a checkout the server made, by the SHA-256 digest of its token, never the token.
  `reader_plans.paddle_price_id`, written with the rest of the plan, so the account sheet can say
  monthly or yearly (nothing else told them apart; a plan applied before it has NULL until its
  next message or the regular check). `paddle_plans_to_check(size)`, a security definer only the
  billing role may execute: up to `size` stored subscription ids of accounts not erased, live or
  changed in the last 30 days, in random order. The billing role gains `SELECT, INSERT, UPDATE`
  on `paddle_checkouts` (no `DELETE`; the purge takes an erased account's rows) and `EXECUTE` on
  the function; the deployment's check holds it to that both ways.
- **The binding: a checkout token.** `POST /api/billing/checkout` makes 32 random bytes, keeps
  their digest for the reader for seven days, and puts the token beside the reader's id in the
  transaction's custom data (`mothli_reader`, `mothli_checkout`). Paddle copies it to the
  subscription ([custom data](https://developer.paddle.com/build/transactions/custom-data)), so
  every message and every `GET` of it carries it. A subscription Mothli has not stored becomes the
  named reader's only with a token kept for that same reader, unused and unexpired (since
  "One Paddle account, several products" below: unused, however old); it is used up
  in the transaction that first applies the subscription. Not chosen: binding by the stored
  transaction id, because Paddle puts `transaction_id` only on `subscription.created`; the page
  says no other subscription event carries it and the API's subscription entity does not either
  ([subscription.created](https://developer.paddle.com/webhooks/subscriptions/subscription-created)).
  Paddle does not keep the order of delivery, so `subscription.activated` or `.updated` can come
  first, and the regular check could never repair a binding it cannot see. Step 3's path by custom
  data alone no longer makes anyone paying: such a message is recorded `no_account` and changes
  nothing. One consequence: a late message about a reader's old, replaced subscription (its token
  used) is now `no_account` where step 3 recorded `other_subscription` or `older`; it changes
  nothing either way. Seven days covers a checkout left open overnight and Paddle's retries of a
  first message (about two days live); later dropped as a condition (below), the seven days are
  now only a record in `expires_at`. The digest is written before Paddle is asked, so no
  transaction exists whose token Mothli did not keep; a checkout Paddle then fails is never used
  and expires. A reader's own browser may see their token (Paddle.js shows the checkout's custom
  data); it binds to them alone.
- **No customer is set on the transaction.** Section 7 said "with the reader's email as the
  customer"; [create a transaction](https://developer.paddle.com/api-reference/transactions/create-transaction)
  takes no email, only a `customer_id`, and making or finding a customer by email could reach a
  MySigner customer in the shared account. So the transaction is a draft with Mothli's price and
  the custom data; Paddle's checkout asks for the email, and the next pull request can prefill it
  in the browser with Paddle.js `customer.email`
  ([prefill](https://developer.paddle.com/build/checkout/prefill-checkout-properties)). A reader
  who is also a MySigner customer under the same email is matched to that customer by Paddle; the
  portal for that customer then shows both products' subscriptions, to the same person.
- **The routes** (`backend/app/hosted/subscribe.py`), each signed in, each for the session's reader
  only (no route takes an account, subscription or customer id), each writing through the billing
  role: `GET /api/billing/plan` (always there in hosted mode; not folded into the session, which
  gains `mothli` in step 5) answers `state`, `why`, `plan` (`monthly`, `yearly`, `gift` or null),
  `next_date`, `subscribed`, `cancel_scheduled`, `payment_retrying`; no amount, no count.
  `POST /api/billing/checkout` (`{"plan": "monthly" | "yearly"}`, the plan's first price id),
  `/cancel`, `/keep`, `/portal`, with the CSRF token, exist only with `PADDLE_API_KEY`. Checkout is
  refused while the reader has a live Paddle plan (`409 already_subscribed`), and past ten a day
  (`429 checkout_busy`; each is a call to Paddle). A gifted reader may subscribe; the gift still
  wins while it lasts and the Paddle plan pays from that day (open point 9, choice a: no trial).
  Cancel and keep apply Paddle's answer at once through the message code (newer only, Mothli's
  prices only), so the sheet need not wait for the message. Paddle unreachable, slow (3 seconds to
  connect, 10 to answer), overloaded or refusing: `503 paddle_unavailable` with a kind line, and
  nothing written. The 30-minute rule: Paddle answers `409 subscription_locked_renewal`
  ([error](https://developer.paddle.com/errors/subscriptions/subscription_locked_renewal)); Mothli
  answers `409 try_again_soon` ("Your plan is renewing right now. Please try again in a few
  minutes."), for any `subscription_locked_*`. The portal link is answered once, never stored or
  logged ([portal sessions](https://developer.paddle.com/api-reference/customer-portals/create-customer-portal-session)):
  the card update link for a live plan, the overview otherwise.
- **The client** (`backend/app/hosted/paddle_api.py`): `httpx`, no SDK, no retries (a request that
  may have reached Paddle is never sent twice), five methods and no way to list or search; every
  id checked against Paddle's format before it goes in a path. `PADDLE_API_KEY` (a secret; prefix
  `pdl_sdbx_apikey_` or `pdl_live_apikey_` matching `PADDLE_ENVIRONMENT`, no whitespace, nothing
  more) requires `PADDLE_WEBHOOK_SECRET`. The logs hold one fixed line per failure.
- **The regular check** (`python -m app.hosted.paddle_check`), from the crontab in the web
  container, hourly in the runbook: the simpler choice, because that container already holds the
  key, the billing address and the prices, and it needs no new service; the worker was step 6's,
  built alongside. A page of 100 by default, a second between calls, stops at the first sign Paddle is
  down; it fetches only ids `paddle_plans_to_check` hands it and applies them as messages.

Where Paddle's documentation (read 23 November 2026) differs from this record, or adds to it:

- **Reconciliation lists by customer** in Paddle's guide ("lists subscriptions for active
  customers via the API",
  [provision access](https://developer.paddle.com/build/subscriptions/provision-access-webhooks)).
  Not followed: the shared account. The check fetches stored subscription ids one by one instead;
  section 11's "and the reader's customer's other subscriptions" is dropped for the same reason.
- **The key's permissions are per kind of entity, not per product**
  ([permissions](https://developer.paddle.com/api-reference/about/permissions)): a Mothli key can
  reach MySigner's transactions and subscriptions. The code is the guard, and the tests check
  every request. Needed: `transaction.write`, `subscription.write` (includes read),
  `customer_portal_session.write`; section 7's list, with no customer permission.
- **Keys expire**: 90 days by default, a year at most, and cannot be renewed
  ([authentication](https://developer.paddle.com/api-reference/about/authentication)). Section 7
  did not say; the runbook has the owner note the date.
- **Undoing a cancel** is `PATCH /subscriptions/{id}` with `scheduled_change: null`, as section 7
  says ([cancel subscriptions](https://developer.paddle.com/build/subscriptions/cancel-subscriptions));
  the 30-minute rule's answer is the error page above (the cancel pages do not name it).
- **Rate limits**: 240 requests a minute for each address, 429 `too_many_requests` with
  `Retry-After` ([rate limiting](https://developer.paddle.com/api-reference/about/rate-limiting)).
  Mothli answers a 429 as Paddle unavailable and does not retry.
- **`Paddle-Version: 1`** is sent ([versioning](https://developer.paddle.com/api-reference/about/versioning));
  the addresses are `https://sandbox-api.paddle.com` and `https://api.paddle.com` (same page).

Unproven until a sandbox run: that a draft transaction with no customer opens in Paddle.js with
its id and becomes a subscription carrying the custom data; whether Paddle.js lets the browser
change a server-made transaction's custom data (it cannot learn another reader's token either
way); the exact error Paddle gives a cancel inside the 30 minutes, and for a cancel of a
`past_due` plan; the portal answer's real shape and how long its link lasts; that a cancel's and
an undo's answers carry an `updated_at` newer than the message they cause; the dashboard's names
for the key's permissions; the regular check, which has never run from a crontab in the web
container; and the walk with test cards that section 11 asks for.

**Step 4, screens, built (2026-11-23).** The plan sheet and the plan part of the account sheet.
Nobody has seen either in a browser yet, and nothing was run against Paddle: Paddle.js is a fake
`window.Paddle` in every test.

- **What the browser is told.** `GET /api/billing/plan` gains `manage` (whether the cancel, keep
  and portal routes exist: `PADDLE_API_KEY` is set) and `checkout` (`checkout_setup`: the
  environment, the client token and the price sold for each plan, the first id of each setting;
  null without the key, the token or a price). Folded into the plan rather than a route of its
  own: the sheet needs both together, and one request is one less to fail. New setting
  `PADDLE_CLIENT_TOKEN`: public by design
  ([client-side tokens](https://developer.paddle.com/paddle-js/about/client-side-tokens): "safe to
  publish"), still a setting; `test_` for the sandbox, `live_` for live, then anything but
  whitespace; requires `PADDLE_API_KEY`. Tests hold that no answer carries the API key, the
  webhook secret or the billing address.
- **Paddle.js** (`frontend/src/shelf/paddle.ts`) is added as Paddle's own script tag from
  `https://cdn.paddle.com/paddle/v2/paddle.js`
  ([include Paddle.js](https://developer.paddle.com/paddle-js/about/include-paddlejs)), only when
  the plan sheet opens; no package. `Paddle.Environment.set("sandbox")` first in the sandbox
  ([environment](https://developer.paddle.com/paddle-js/methods/paddle-environment-set)), then
  `Paddle.Initialize` once a page with the token and one event callback
  ([initialize](https://developer.paddle.com/paddle-js/methods/paddle-initialize)). Prices come from
  `Paddle.PricePreview` with both price ids and no address, so Paddle places the reader by their
  connection and answers in their currency with tax
  ([price preview](https://developer.paddle.com/paddle-js/methods/paddle-pricepreview); amounts in
  the currency's smallest unit, [preview prices](https://developer.paddle.com/api-reference/pricing-preview/preview-prices)).
  Shown: Paddle's own formatted totals; the yearly plan as a month is its total over twelve,
  rounded to the smallest unit and formatted for the currency; the saving is
  `floor((12 × monthly − yearly) × 100 / (12 × monthly))` percent, shown only when it is at least 1.
  At $12.99 and $79.99: "$6.67 a month, billed yearly" and "$79.99 a year, 48% less than paying
  monthly." Checkout: `Paddle.Checkout.open({transactionId, customer: {email}, settings:
  {displayMode: "overlay", locale: "en"}})`
  ([checkout open](https://developer.paddle.com/paddle-js/methods/paddle-checkout-open)); the
  reader can change the email there. `checkout.completed` and `checkout.closed`
  ([events](https://developer.paddle.com/paddle-js/events)) start the wait: the sheet asks
  `GET /api/billing/plan` every two seconds until `subscribed`. After a completed payment ("One
  moment, I’m getting ready…") it asks for 60 seconds, then says the payment may still be on its
  way and to reopen Your account. After a checkout only closed ("One moment…") it asks for 8
  seconds, in case the completed event was missed, then shows the two plans again with "No
  worries, nothing has changed. Pick a plan whenever you like."
- **The browser's security policy.** Paddle publishes no list of addresses (searched its developer
  pages and its whole documentation text, 23 November 2026). Read instead from Paddle.js 2.13.0
  itself: the checkout window is `https://buy.paddle.com` (sandbox `sandbox-buy.paddle.com`), the
  price preview `fetch`es `https://api.paddle.com/pricing-preview` (sandbox `sandbox-api`), and
  `Initialize` adds the stylesheet `https://cdn.paddle.com/paddle/v2/assets/css/paddle.css`
  (sandbox `sandbox-cdn`). Allowed exactly those, in `backend/app/main.py`, `deploy/Caddyfile` and
  `frontend/vite.config.ts`: `script-src` gains `https://cdn.paddle.com`; `style-src` gains
  `https://cdn.paddle.com https://sandbox-cdn.paddle.com`; `frame-src` gains
  `https://buy.paddle.com https://sandbox-buy.paddle.com`; `connect-src` gains
  `https://api.paddle.com https://sandbox-api.paddle.com`. One policy for both environments,
  because the Caddyfile's is fixed text. Not allowed: `https://public.profitwell.com`, the script
  Paddle.js adds with a live token for Paddle Retain, which Mothli does not use; the browser
  blocks it and the checkout does not need it.
- **Where the plan sheet opens**: from the account sheet only. The chat's, the cast's and
  "Story so far"'s invitations (step 5) render `<PlanSheet email={session.user.email}
  onClose={...} />` from `frontend/src/shelf/PlanSheet.tsx`; it reads the plan itself (or takes
  `plan`), and `onJoined(plan)` is called once the server says the reader is on it. The opener
  puts focus back.
- **Choices made**: the account sheet speaks of a New reader too ("Talking with Mothli is free
  for your first two weeks, starting the first time you use it.") and of each way to be
  Ended, each with "See the plan"; section 6 named only the free weeks' line. A payment being
  retried shows "Cancel my plan" beside "Update payment details", so cancelling is never hidden. A
  gifted reader who also holds a plan sees the gift line, then "You have a plan of your own too."
  (or "Your plan won’t renew." once cancelled) and its buttons; their cancel confirmation names
  no day, because the plan's renewal is not in the answer while the gift is. Following open point
  9 (a), the plan sheet says "Join now and your plan starts today." in the free weeks and under a
  gift. No refund line: a commented place in the sheet waits for open point 8.

Not verifiable without a browser and Paddle's sandbox: that the overlay opens with a server-made
draft transaction and the prefilled email; the price preview's real answer; that the policy is
enough (a blocked address shows only in the browser's console); that `Referrer-Policy:
no-referrer` does not trouble Paddle's domain check (Paddle.js passes the page in its own
`source_page` parameter); the order of `checkout.completed` and `checkout.closed`; the portal tab
on phones; and how either sheet looks.

**Step 6 built (2026-11-23).** Background work follows where a reader stands (section 5). No
migration. Settings `HOSTED_WARMUP_DAYS` (30), `HOSTED_WARMUP_NEW_CHAPTERS` (25) and
`HOSTED_WARMUP_NOTES_USD` (0.50), carried with the other caps to the web process and the worker
(which now also carries `HOSTED_FREE_DAYS` and `HOSTED_FREE_USD`, so both read the same state).
The worker role gains `SELECT` on `reader_plans`, `EXECUTE` on `reader_plan_state`, and `SELECT`
on two columns of `users`, `id` and `created_at`, and on no other column (`grants.py`,
`hosted_roles.py`; its startup check verifies every column of `users` both ways and the function).

- **At the claim** (`backend/app/hosted/jobs.py`, `background_held`): after the existing
  background guard (a capped reader's notes still move to the next UTC day), a companion job of
  any kind whose reader is `ended`, or `new` and past the warm-up, is finished without work and
  without spending, as a cancelled job is (state `cancelled`, no attempt, the handler never
  called, so nothing is read). `free` and `paying` readers are not looked at further. The new
  reader's three bounds are read in the same statement as the state: sign-up within the days; the
  reader's background ledger and open background reservations, of any age, against the notes
  amount (before a first question that is their chapter notes alone, recollections coming only
  from replies); and their story-memory jobs that published, ever, against the chapters. Past any
  of the three, all of their background work stops, as for Ended.
- **In the story-memory run**: just before a run would be the first to build a chapter (where the
  ADR 0044 guards are applied, which still follow), it asks the same again and, if held, ends
  without spending or asking for its next run.
- **In the routes**: a finished chapter, "I've read this" and the first page read ask for no job
  for an Ended reader (the page itself is recorded as before); a question from an Ended reader
  passes no notes queue or recollection queue to the reply. A question from anyone else asks for
  the book's story memory, and the reply asks for its chapter notes as before, so a reader who
  pays again (or a new one, whose question just started the free weeks) has both built up to
  their place: chapter notes for the chapters fully read, story memory to the target the read
  map gives (one chapter past the last finished), served only through their place. The routes do
  not look at a new reader's warm-up (the tenant role holds nothing on `users`): such a reader's
  pages still ask, and the claim finishes those jobs.
- **Work under way is finished.** The claim decides whether a run happens; a run it let start
  finishes, whatever the reader's state becomes meanwhile, as a reply under way does. So a bound
  can be passed by the runs under way when it is met: a notes run is at most ten chapters (about
  $0.07), a story-memory run one chapter, and one reader has at most two jobs running at once by
  default (`owner_limits.max_provider_concurrency`). **The warm-up's most per sign-up who never
  asks**, at section 5's figures: $0.50 + 25 × $0.09 = $2.75, plus at most two runs under way when
  a bound is met (about $0.16 more at worst), and only for a file nobody else has.
- **"Could not verify" 9, settled**: job rows are never deleted except with the account (the
  purge) and by migration 0028 (the retired `ingest_book` kind only); neither the tenant nor the
  worker role holds `DELETE` on `jobs`, and removing a book keeps its jobs (cancelled). So a
  reader's story-memory jobs that published are kept for the life of the account, and counting
  them over all time is the count over the whole time a reader has been new (no other state comes
  before `new`).
- **Two existing tests changed, no assertion changed**: in the ADR 0044 chapter-guard test and the
  abuse case (`test_story_memory_one_ahead_postgres.py`) the reader never asks, so is now New and
  would meet the warm-up's 25 chapters before the guards' 30 and 300; both now start the reader's
  free weeks first. The run's repository call is listed in that file's spoiler test (`REACH`) as
  reading nothing of an edition. Nothing a reader is served changes in this step (that is step 5).

**Step 5, server half, built (2026-11-23).** The gate, the start route and the session's
`mothli`; no migration, no new setting. The invitations, the hello, the ending note and the
sign-up line are the screens' (the next pull request).

- **The gate** (`backend/app/hosted/companion/gate.py`, and two dependencies in `routes.py`,
  `_mothli` and `_csrf_mothli`). A route behind it takes its reader through the dependency, which
  reads `reader_plan_state` alone and, for an ended reader, answers `402` with
  `{"detail": {"code": "mothli_plan", "why": "time" | "ceiling" | "plan"}}`, the shape of the
  app's other coded refusals (the pending deletion's, the plan routes'), with no message: the
  screens choose the words by why. As a dependency it runs before the route's body, so before the
  reader's catalog, source, search or story memory are opened, before the free weeks are stamped
  and before anything is reserved; after the session, the request limit and (for a question) the
  CSRF check. It depends on nothing but the reader's own row and chat: the same answer for a book
  of theirs, another reader's or none. A database that cannot be reached is `503`, never a yes.
- **Gated**: `POST /api/books/{id}/ask/stream`, `GET /api/books/{id}/conversation`,
  `GET /api/books/{id}/graph` (the cast), `GET /api/books/{id}/character/{entity}` (a person's
  card), `GET /api/books/{id}/notes` ("Story so far"), `GET /api/books/{id}/ingest` (its
  progress), `POST /api/reader/ask/stream` (the home chat), `GET /api/reader/conversation`.
  **Not gated, on purpose**: everything that is reading (the books, the file, the manifest, the
  place and its reset, preferences, search in the book, highlights, notes and bookmarks, the
  library and the free library, the read map, page reports, "I've read this", the read
  frontier), clearing a conversation (forgetting is always allowed), the profile (reading status
  and the reader's own reactions), "What Mothli remembers" (section 6: the reader's own data, to
  read and to forget), the account, the copy of one's data, `GET /api/billing/plan`, and the start
  route. **The stored conversations are gated**: section 6 puts the invitation in place of the
  chat panel's content and section 9 has them "shown through the same filter when they return";
  they are kept, and in the copy of one's data meanwhile. A non-database test lists every hosted
  route on one side or the other and checks the dependency stands on exactly the gated ones.
- **A reply under way finishes; a reply that meets the gate in the stream says so.** A reader
  who ends between the gate and the reply's first call (another reply reached their ceiling, the
  two weeks ran out, a plan stopped) is refused there by `reserve_cost` with `PlanNeeded`, whose
  error event is `{"type": "error", "status": 402, "code": "mothli_plan", "why": ..., "message":
  "Mothli needs the plan"}` (the message is for the logs). A reply's later calls are never
  refused, as before.
- **The free ceiling and the stopgap.** Step 1's line at the ceiling (`_FREE`) is removed: at the
  ceiling a reader is ended with why `ceiling`, and the gate's one answer is theirs, in a book and
  at home. After day 15 a reader who does not pay is ended with why `time` and gated, no longer
  held by the paying readers' day and month. Those two now hold paying readers (with the first
  paid month), readers in their free weeks whose own `owner_limits` caps are set, and readers who
  have not started. **A choice**: an ended reader is refused whatever their own `owner_limits`
  caps. Step 1 let an operator's raised caps carry a reader past the free ceiling; with the gate
  that would be two answers for one state (the session and the routes saying ended while the
  chat answered), so a tester the owner wants past it is given a gift (step 2).
- **`POST /api/reader/mothli/start`** (signed in, CSRF; `404` where this server has no chat),
  body `{"book_id": "<uuid>"}` or `{}`. Unless the reader is ended it calls `start_free_weeks()`
  exactly as a question does (once; a gifted reader is stamped too) and, with a book, asks for its
  chapter notes and story memory as a question does since step 6 (a book not the reader's adds
  nothing). An ended reader is stamped with nothing and asked for nothing. It answers `200` with
  the session's `mothli`. The app's own fetches stay plain reads: allowed for `new`, `free` and
  `paying`, refused for `ended`, and they never start the clock.
- **The session** (`GET /api/auth/session`) gains `"mothli": {"state": "new" | "free" | "paying"
  | "ended", "why": "time" | "ceiling" | "plan" | null, "ending_note": true | false}`, or `null`
  where the server has no chat or the plan cannot be read just then (the gate still answers every
  route itself). `ending_note` is true in the free weeks' last two days (from the database's
  clock). **Not duplicated**: the plan's kind and dates (`plan`, `next_date`, the free weeks' end
  the note names) stay `GET /api/billing/plan`'s, which the screens read when the note or the
  account sheet shows; the sign-in screens need only the state. No amount, no count.
- **What an ended reader sees in today's app**, until the screens land: an ordinary failure, no
  invitation and no button. The chat in a book says "I couldn’t get a reply. Try again?", the
  home chat "That didn’t go through. Your question is still here.", the cast "I couldn’t gather
  the cast right now. Try again in a moment.", "Story so far" "I couldn’t open my notes right now.
  Try again in a moment.", the people list in the chat "Couldn’t find the names. Try again?";
  names in the page are not looked up, and the corner note after a chapter still offers "Want to
  hear what I noticed?", whose "Tell me" meets the same chat failure. Reading is untouched. Before
  this step the same reader past the ceiling saw step 1's ceiling line, and past day 15 was
  answered up to the day and month.
- **Existing tests changed** to the decided behaviour: in `test_free_weeks_postgres.py`, day 15
  now meets the gate (was today's day line), the ceiling's next request meets the gate in a book
  and at home (was the ceiling line), the adapter's refusals at the ceiling and after day 15 are
  the gate's (were `free` and the day and month), an operator's raised caps no longer carry a
  reader past the ceiling, and the spoiler test compares free weeks, gift and Paddle (the day-15
  leg, now the gate's, moved to `test_mothli_gate_postgres.py`) and gained the home chat and the
  cast and "Story so far" served; in `test_plans_postgres.py`, a gift that ends past the ceiling or
  past day 15 meets the gate; in `test_paddle_messages_postgres.py`, a reader who paid and stopped
  meets the gate (was the day); in `test_background_by_state_postgres.py`, the ended reader's cast
  and progress are the gate's, then equal to the paying reader's once they pay. No spoiler test is
  weakened or removed. `endpoints.json` lists the start route (version 26).

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
   month ceiling. Decided by the owner on 23 November 2026: $1.00 a day and $4.00 a month, and
   $6.30 for a subscriber's first paid month only (section 4). No number is ever shown.
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

**No number shown, ever.** Two hidden guards, reusing the existing settings and lines. **Decided by
the owner on 23 November 2026**: about 47 replies a day, about 190 a month, and about 300 in a
reader's first paid month only; at 2.1 cents a reply, $1.00 a day, $4.00 a month and $6.30 for the
first paid month.

- **A day guard**: `HOSTED_READER_USD_PER_DAY`, **$1.00** (about 47 replies; proposed $0.60, decided
  $1.00). Past it, the reader sees today's line, unchanged: "We’ve talked a lot today, so I’m resting
  until tomorrow. Your book and your place are right here." (`spend.py:212-213`).
- **A month ceiling**: `HOSTED_READER_USD_PER_MONTH`, **$4.00** (about 190 replies; decided as
  proposed). Past it, the reader sees today's month line, unchanged: "We’ve talked our way through
  this month, so I’ll be back at the start of the next. Your book and your place are right here."
  (`spend.py:214-215`). No number, no date of a reset counter; "the start of the next" is the 1st of
  the UTC month, as the code counts.
- **The first paid month**: **$6.30** (about 300 replies) in place of the $4.00 month, once per
  subscriber, counted from when their first paid plan starts. Decided, **not built in step 2**: it
  needs to know when a Paddle plan started, so it belongs to step 3. A gift has no first month: a
  gifted reader has $1.00 a day and $4.00 a month.

The two defaults are built (step 2): `HOSTED_READER_USD_PER_DAY` 1.00 and
`HOSTED_READER_USD_PER_MONTH` 4.00 in `backend/app/config.py`, `SpendLimits` and
`deploy/app.env.example`. The free weeks are unchanged: $2.10, no day stop.

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
day guard of $1.00 means the month ceiling can only be reached by talking a lot on at least four
days. Decided (23 November 2026): $1.00 a day and $4.00 a month, as the table's middle row, then tune
both with real use. The first paid month's $6.30 is once per subscriber: at full use that month leaves
$7.33 − 6.30 − 0.42 = **$0.61 (8%)** of a monthly plan, and a yearly subscriber's first month is
carried by the rest of their year.

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
- Gifted readers have the paying readers' hidden guards: $1.00 a day and $4.00 a month, with no
  first-month ceiling (section 4). The owner can lift his own with the existing `owner_limits`
  override (`limits.py`).

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
3. **Paddle's messages.** Migration `0039_paddle_events.sql` (numbered 0038 when this record was
   written; 0038 went to the gifts of step 2): `paddle_events` (event id, type, time,
   account, outcome; no payload kept); the `billing` role in `hosted_roles.py` and `grants.py`;
   `POST /api/billing/paddle` (signature, time window, duplicates, order, matching). Settings
   `PADDLE_ENVIRONMENT` and `PADDLE_WEBHOOK_SECRET`. Tests first, with messages signed in the test: a bad
   or missing signature writes nothing; an old timestamp is refused; a repeat is ignored; an older message
   after a newer one changes nothing; `active`, `past_due`, scheduled cancel, `canceled`, `paused` give the
   states of section 1; an unknown account is recorded and ignored; the billing role cannot read any other
   table. **And the first paid month** (section 4, decided 23 November 2026): the first time a
   Paddle plan makes a reader paying is kept, once per account and never moved, and for the month
   that follows the reader's month is $6.30 in place of `HOSTED_READER_USD_PER_MONTH` (a setting,
   `HOSTED_FIRST_PAID_MONTH_USD`); the day stays $1.00, and a gift never gets it. Tests: a first plan
   gets the larger month and a second plan after a gap does not; a gifted reader does not; the day
   line and the month line are today's. **Reader sees:** nothing yet.
4. **Subscribe, cancel, payment details.** `POST /api/billing/checkout`, `/cancel`, `/keep`, `/portal`
   (signed in, CSRF), through the billing role and Paddle's API with `PADDLE_API_KEY`; the plan sheet and
   the account sheet's part (section 6); Paddle's script and window allowed in the three security
   policies; `deploy/app.env.example` gains the Paddle lines as placeholders. Tests: the server, never the
   app, chooses the account in the custom data; the app shows prices it was given and none of its own;
   cancel schedules at the period's end; frontend tests for every line of the account sheet. Then a walk
   through the sandbox by hand with Paddle's test cards, written up in the pull request. **Reader sees:**
   they can subscribe and cancel from Your account. Added with step 3 (23 November 2026):
   - **A new subscription is bound to a checkout the server made**, not to custom data alone.
     Paddle.js lets any checkout carry any custom data, so the server stores, for the reader, the
     transaction it created (its id), and a new subscription becomes that reader's only when it
     came from that transaction (how Paddle links the two, for example the transaction's
     `subscription_id`, is to be confirmed in the sandbox); one that names a reader but came from
     no checkout of theirs is recorded and ignored.
   - **A reconciliation against Paddle's API**, as Paddle's provisioning guide recommends: on a
     schedule, and for one reader on demand, the server reads the stored subscription (and the
     reader's customer's other subscriptions) from Paddle and applies them as a message would, so
     a lost, refused or out-of-order message is repaired without waiting for the next renewal.
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
7. **The ceilings in numbers.** **Decided by the owner on 23 November 2026**: paying, $1.00 a day
   (about 47 replies) and $4.00 a month (about 190), and $6.30 (about 300) for a subscriber's first
   paid month only; free, $2.10 (about 100 replies) as before. (Proposed were $0.60 a day and $4.00 a
   month; section 4's table: $3.00 is safer for the yearly plan, $5.00 loses money on a heavy yearly
   reader.)
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
- A new database table and role, three migrations (the gifts took one of their own), one Paddle message route, four small billing routes, one
  operator command, and three public pages.
- The owner holds three new secrets and two price ids. No secret is in the repository.
- No layer of spoiler protection changes.

## What this record could not verify

1. Whether Paddle's rate for a $9.99 plan differs from 5% + $0.50, and whether its $10 is counted before or
   after tax (only Paddle can say).
2. That a refund does not end the subscription by itself: no Paddle page read says either way.
3. Whether Paddle's emails to buyers can be turned off or reworded; nothing here depends on it.
4. The exact addresses Paddle's script and checkout window need in the browser's security policy.
   Read from Paddle.js itself with the screens of step 4 (see "Step 4, screens, built"); still to
   be seen working in a browser.
5. The shape of `custom_data` in a real message (an older Paddle page describes it as text holding JSON);
   to be seen in the sandbox in step 3.
6. How long a customer portal link lasts (Paddle says the token is temporary, no figure).
7. Whether Paddle approves an AI reading companion.
8. Whether Paddle's checkout records the EU buyer's consent to start using digital content at once, which
   decides the 14-day withdrawal right; legal advice before launch is worth it.
9. That old `story_memory` job rows are kept long enough to count a New reader's first builds over 30 days
   (`first_builds` reads them; their removal was not traced). Settled with step 6: they are kept for
   the life of the account (see "Step 6 built").
10. Every money figure: 2.1 cents a reply, 0.7 cents a chapter of notes and 7 to 9 cents a chapter of story
    memory come from earlier measurements and records, not from a run for this record.

**One Paddle account, several products: Mothli's own payment link (2026-11-23).** The owner sells
Mothli and MySigner from one Paddle account, and it stays so (settled). Paddle supports it ([one
account](https://www.paddle.com/help/start/set-up-paddle/can-i-have-multiple-paddle-accounts):
"You can sell all your products and services from one Paddle account"; one legal name, display
name and statement descriptor per account). The account's default payment link (**Checkout >
Checkout configuration**) is MySigner's pricing page, and its only approved website MySigner's;
neither is changed for Mothli. Nothing was run against Paddle: it is stood in for in every test.

- **Every transaction carries Mothli's page.** `POST /transactions` now sends
  `checkout: {url: PADDLE_CHECKOUT_URL}`. A transaction's `checkout.url` overrides the default
  payment link for that transaction ([default payment
  link](https://developer.paddle.com/build/transactions/default-payment-link): "You can pass any
  of your other approved websites as checkout.url when creating or updating a transaction to
  override the default checkout link"); writable on create and update, approved domains only,
  sandbox domains approved by themselves ([checkout domains,
  2023](https://developer.paddle.com/changelog/2023/checkout-domains/)). New setting
  `PADDLE_CHECKOUT_URL`: `https://`, a host in `TRUSTED_HOSTS`, the front page only (no path,
  query, fragment, port or user information: the proxy serves the app at `/`, and Paddle adds
  its own `?_ptxn=`); required with `PADDLE_API_KEY`, so a deployment cannot fall back to the
  other product's page; refused without it. Paddle's refusal of an unapproved domain
  (`transaction_checkout_url_domain_is_not_approved`, 400,
  [error](https://developer.paddle.com/errors/transactions/transaction_checkout_url_domain_is_not_approved))
  is a refusal like any other for the reader (503 `paddle_unavailable`, "I can’t reach our payment
  service just now…") and one fixed log line for the operator: `paddle refused the checkout
  address: approve the domain of PADDLE_CHECKOUT_URL in Paddle under Website approval`.
- **The page.** Paddle's emails about a checkout link to `checkout.url` with `_ptxn` set to the
  transaction id. Opened with exactly one well-formed id (`txn_` and 26 lowercase letters or
  digits), the app reads it once, takes it out of the address before Paddle.js loads (so
  Paddle.js never opens a second checkout of its own, and a reload opens none), and opens that
  checkout with `Paddle.Checkout.open({transactionId})`, which "takes priority over the query
  parameter". Anything else in `_ptxn` is ignored: nothing read, nothing loaded. Paddle.js still
  loads on no other screen.
  - *Signed in:* the plan sheet opens on top of the shelf ("One moment, I’m opening your
    checkout…"), asks the plan first (a reader already on it is told so and no checkout opens),
    opens the checkout with their email, then the same wait-and-confirm as from "Choose yearly".
  - *Signed out:* the checkout opens anyway over the welcome, with a calm line; once paid, "Thank
    you! Sign in and you’ll find your plan waiting in Your account."; once closed unpaid, "No
    worries, nothing has changed. Sign in and I’ll open your checkout again." (signing in comes
    back to it). Chosen over signing in first because it cannot lose a payment: the transaction
    already names the reader who started it and that checkout's token (custom data, made by the
    server), so whoever is or is not signed in on this device, the payment binds to the reader it
    was made for. Signing in first can stand between a willing reader and their checkout (a
    sign-in that fails, or another account). The page reads only `GET /api/billing/payment-link`,
    public, answering exactly the environment and the client token (404 where no checkout opens).
- **Paddle's "update your payment method" email.** The default payment link page says "The link
  to update a payment method redirects to your default payment URL to open a checkout to update
  a payment method", and the subscription update-payment-method transaction's example
  `checkout.url` is the default link ([update payment
  details](https://developer.paddle.com/build/subscriptions/update-payment-details)); neither page
  says a transaction's own `checkout.url` carries over. So in this shared account that email
  sends a Mothli subscriber to MySigner's page. Not changed here (it is an account setting).
  Mothli's screens point readers to **Your account > Update payment details** (the customer
  portal for their own subscription, built in step 4), and the runbook says so.
- **The content security policy is unchanged**: the same outside addresses as before.
- **A checkout token binds however old it is.** Until now a token bound its reader only for
  seven days (`expires_at`). A payment made later, from Paddle's reminder email for example,
  would then have bound to no one: a reader who paid and is not paying, which the regular check
  cannot repair (it fetches only subscriptions Mothli has stored). The expiry bought nothing: the
  token is 32 random bytes, kept for one reader and used up once. So the binding now asks only
  that the token be the named reader's and unused (`billing._checkout_open`); `expires_at` is
  still written, as a record, and decides nothing. No migration: `0040` is as it was. Another
  reader's token, a used token and no token still change nothing, and the ten checkouts a day
  stay.
- **Still to be seen in the sandbox:** the transaction accepting `checkout.url` on the sandbox
  domain, Paddle's abandoned-checkout email linking to `https://<domain>/?_ptxn=…`, the checkout
  opening from it signed in and signed out, and the payment binding to the reader.

**Step 5, screens, built (2026-11-23).** What a reader sees of the free weeks and of the gate,
frontend only. Section 6's lines are used word for word. Nobody has seen any of it in a browser
yet.

- **Where the reader stands, in the app** (`frontend/src/ui/mothliStanding.ts`): one copy of the
  server's last word. It starts from the session's `mothli` (null in local mode, without a chat,
  when the plan cannot be read, and offline, where the saved session carries none). It changes
  without a reload when the server answers any route `402 mothli_plan` (read in the client's one
  error path, `api.ts`), when a reply's error event carries the same code mid-stream, when the start
  route answers, when a new reader's first reply arrives (their question started the free weeks),
  and when the plan sheet says the reader is on the plan (from an invitation, the gentle note or
  the account sheet). The invitation shows only where that copy says `ended`. When it is null, or a
  request fails for any other reason, every panel behaves as before.
- **Ended** (`frontend/src/ui/MothliPlan.tsx`): in the chat in a book, the cast, "Story so far"
  (each tab of the panel; its header and tabs stay) and the home chat (its title and "What Mothli
  remembers" stay), the invitation by why with "Keep talking with Mothli", which opens the plan
  sheet. Nothing of the conversation, the cast or the notes is asked for or shown, including what
  was on screen before the server said so. A question sent anyway, or a stored conversation the
  gate refuses, shows the invitation and no line; the question stays in the book chat's box. The
  invitation's words take focus when they replace content that had it, or when the panel has just
  opened, and never on a page just loaded. On the reading page, the note after a chapter keeps
  "That’s Chapter 3 done!" (and a milestone) and drops the offer, its button and its people. Names
  in the page are not looked up, and any names already marked are cleared. The welcome-back card,
  whose only buttons ask Mothli, is not shown (section 6 does not name it; it is an offer the reader
  cannot have, like the corner note's). Clearing a conversation is not offered while ended: the
  panel's content is the invitation. The server still allows it.
- **Joining**: on the plan sheet's `onJoined` the copy becomes `paying` when the sheet closes. The
  panel comes back, its stored conversation or the cast or the notes are asked for again, and focus
  goes to the chat's box. A sheet closed without joining reads the plan once, in case the reader
  joined elsewhere (the sheet's "already on the plan" says so without calling `onJoined`).
- **New**: the hello replaces the book chat's greeting at the top of the conversation, above the
  box, and stays while the first question is on its way, until the reply arrives. At home it sits
  above the home chat's own first line. **The start call** is made from the panel when the cast or
  "Story so far" tab is shown, which happens only by the reader's tap (a tab, a person in the
  corner note, a name card's "more"). It is also made from "Who was that?", the cast's list in the
  chat. It is made only for a reader the copy says is `new`, and once: the route's answer makes
  them `free`. The app's own cast and notes fetches on the reading page never call it.
- **The gentle note**: at the top of both chats when the copy says `free` with `ending_note`. It
  asks `GET /api/billing/plan` once a page for `next_date` and says when, by the reader's own
  calendar day in their own time zone, as a friend would: "…our free weeks together end today.",
  "…end tomorrow." or "…end on Thursday." (the note shows only in the last two days, where a bare
  weekday said on that very day would be ambiguous). The rest of section 6's line is unchanged; no
  hours, no countdown, no number. It is not said at all if the plan cannot be read. "Got it" is
  remembered on the device (`mothli.ending-note-seen`), for both chats. "See the plan" opens the
  plan sheet.
- **The sign-up line** sits under the welcome's lede. It is not shown on the welcome-back of a
  reader signed out mid-read, nor beside a payment link's checkout (that reader has used Mothli and
  is joining).
- **Keyboard**: the chat panel yields Escape and Tab to the plan sheet over it. Without that,
  Escape in the sheet also closed the panel in the phone layout.

Tests (frontend only): `frontend/src/ui/MothliPlan.test.tsx` (every line word for word, none with
a digit, "unlimited", "questions left", hours or a countdown; the invitation by each why on each of
the four surfaces; a 402 in a reply, mid-stream and on a stored conversation; joining brings each
panel back; the start call from each tap and never for `free`, `paying` or null; the hello until the
first reply; the note, "today", "tomorrow" or the weekday on a fixed clock in time zones whose day
differs from UTC's, and "Got it"; null as before), and additions to `api.test.ts`,
`ReaderPage.lifecycle.test.tsx` (the corner note for an ended reader; the automatic fetches never
start the free weeks), `SignIn.test.tsx` and `PaymentLink.test.tsx`. Not seen in a browser: the
invitation's and the note's look on a phone and in the dark theme, the focus moves with a real
screen reader, the plan sheet opened over the chat panel (inert and focus), and the weekday in real
time zones.

**Step 7 built (2026-11-23).** Deleting an account while paying, and "your plan.json". Nothing was
run against Paddle: the tests' stand-in Paddle, behind the real client, holds the owner's other
product's subscription and customer too, and nothing may touch them.

- **Migration `0041_cancel_owed.sql`.** `reader_plans.cancel_owed_since`: when the reader asked to
  delete their account. From then on the cancel of their plan at the end of the period paid for is
  owed to them whenever the stored plan is live with no scheduled end. `paddle_plans_to_check`
  made again with the same signature and rights, the owed plans first. No new role, right or
  function.
- **Asking to delete** (`POST /api/account/delete`, `auth/account.py`) writes the mark first, on
  the billing role (a row is made for a reader who has none, so a plan a late payment brings to an
  account waiting to be erased is owed it too); then the account is marked as before; then the
  cancel is asked of Paddle through `subscribe.change`, the code "Cancel my plan" runs (one call,
  `effective_from: next_billing_period`, the reader's stored subscription id only). Paddle
  unreachable, slow, overloaded or refusing (`subscription_locked_*` inside the 30 minutes before
  a renewal included) changes nothing for the request: the deletion goes through, the mark stays.
  A database that cannot be reached for the mark refuses the request before anything happens
  (`503 account_busy`; the app's "I couldn’t do that just now…"). A reader with no live plan, or
  one already cancelled, costs no call.
- **The guarantee: the mark and the regular check.** The check (`paddle_check.py`, hourly) takes the
  owed plans first, fetches each, applies it, and while it still shows renewing asks for the
  cancel through the same `change`, every run, until Paddle's answer brings the scheduled end.
  Chosen over a retry by the daily purge because the check already holds the key, the billing
  role and the prices, runs 24 times more often, and is the one place that already calls Paddle on
  a schedule; the purge has no key. Chosen over retrying inside the request because a request
  must never be sent twice to Paddle by the client, and the reader's deletion must not wait on it.
  **What cannot be guaranteed**: a renewal that Paddle starts before the cancel gets through (the
  30-minute lock) is charged; the cancel then takes effect at the end of that new period, and the
  log says `paddle refused an owed cancel while a renewal is under way: ... refund it (runbook)`
  for the operator, who refunds it in Paddle (the runbook's query finds the readers).
- **Changing their mind.** Coming back within the 30 days (`POST /api/account/keep`) leaves the
  mark: the plan still ends at the period's end, and the account sheet shows "Your plan ends on …"
  with "Keep my plan" as before, the check confirming a cancel that was not yet through. "Keep my
  plan" (`POST /api/billing/keep`, on the waiting screen after keeping the account, or on the
  account sheet) clears the mark, then removes Paddle's scheduled cancel if there is one; a cancel
  that never reached Paddle needs no call. A new checkout of the reader's own clears the mark too,
  so a new plan after the old one ended is not cancelled by the old request. If the period ends
  before they come back, they are an ended reader like any other. A race left as is: "Keep my
  plan" pressed while the check is cancelling can end with the plan cancelled; the reader sees
  "Your plan ends on …" with "Keep my plan" again (the safe way round).
- **The waiting screen** reads `GET /api/billing/ending` (signed in in any state, as the copy of
  one's data is; only where the server manages plans): `{"wont_renew", "ends"}`, true for a live
  plan cancelled or owed its cancel, with the end of the period paid for. No id, no amount.
- **Erasure.** The purge takes every row with the account's `owner_id`, `reader_plans`,
  `paddle_events` and `paddle_checkouts` included (tested). **The safeguard**: the purge never
  erases an account whose stored plan is live with no scheduled end. It holds it before the point
  of no return (checked before, and again inside, the step that begins erasing), untouched, logs
  `purge held an account whose Paddle plan still renews: ...` (no id, nothing of the reader),
  counts it as `held` in its line and ends with an error, until the check has the cancel
  confirmed; the next run erases it. Chosen over cancelling immediately from the purge: the purge
  holds no Paddle key and should not; and over erasing everything but the plan row: the row is
  what lets the cancel be asked for and the only trace that Paddle is charging someone. Paddle
  keeps its own records as the seller; Mothli asks it to delete nothing.
- **"your plan.json"** in the copy of one's data (`reader_plans` moves from left out to in the
  copy): `free_weeks_began`; `gift` ("Mothli on the house, for good", or "…until the end of
  2099-06-30 (UTC)", or null); `plan` (null, or `kind` monthly or yearly, `status_at_paddle`,
  `first_paid`, `this_period_ends`, `ends`, and a `sold_by` line saying Paddle sells the plan and
  keeps its own records); `not_to_renew_since_you_asked_to_delete_your_account`. Never Paddle's
  customer, subscription or price ids, a checkout token or digest, or an amount. The read-me
  names the file. `paddle_events` and `paddle_checkouts` stay left out, as payment records.
- **What the reader sees.** On the delete confirmation, a reader with a live plan gets one more
  line: "Your plan won’t renew, so you won’t be charged again." followed by "Change your mind
  before 3 December and Mothli is still yours until then." when the day paid for comes before the
  erasure, or "Change your mind within the 30 days and Mothli is still yours until 3 August."
  when it comes after; under a gift, the first sentence alone (the day the sheet knows is the
  gift's). On the waiting screen: "Your plan won’t renew. It’s paid for until 3 December." (or the
  first sentence alone without a day), and "Keep my plan" beside "Keep my account", which keeps
  the account, then the plan, then opens the app. If the plan cannot be kept just then: "Your
  account is back, but I couldn’t keep your plan just now. Try again in a few minutes, or keep it
  from Your account." No number of questions, no countdown, never "unlimited".

Tests: `backend/tests/hosted/test_delete_while_paying_postgres.py` (the cancel for the deleting
reader alone, by their stored id, MySigner untouched; no call without a live plan; Paddle timing
out, down, overloaded, refusing, and locked for a renewal, at the request, then the check while
still away and once back; coming back before and after the retry, "Keep my plan" from the waiting
screen and the account sheet; a late payment on an account waiting to be erased; a new checkout
after coming back; the purge holding while the cancel is unconfirmed, then erasing every plan
row; "your plan.json"; the waiting screen's read), `test_migrations.py` (0041), `test_billing.py`
(the check's owed cancel, non-database), and the frontend's `Account.test.tsx` and
`SignIn.test.tsx`. Not verified: any of it against Paddle's sandbox (that a cancel's answer
carries the scheduled change, the exact refusal inside the 30 minutes), the screens in a browser,
and the purge and the check from their crontabs.
