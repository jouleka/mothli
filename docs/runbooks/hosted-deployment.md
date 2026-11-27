# Running Mothli on your server

Plain steps for running the many-reader version of Mothli on one server behind Cloudflare.
Every command is typed on the server, in the folder where Mothli is checked out (`~/mothli` below),
unless a step says otherwise. Words in CAPITALS inside a command are yours to replace.

How it fits together:

```
reader --HTTPS--> Cloudflare --tunnel--> tunnel --> proxy --> web --> database
                                                  (the app's pages)   worker --> database
```

Nothing on the server listens to the internet. The `tunnel` container calls out to Cloudflare,
and Cloudflare sends readers through it. Cloudflare handles the HTTPS certificate. The database
is reachable only by the Mothli containers.

## 1. What to prepare

You need these before the first deployment. Write each value into the two settings files in
step 2, never into an email, a chat or a document.

1. **A server** (VPS) running Ubuntu 24.04 or Debian 12, with at least 2 CPUs, 4 GB of memory
   and 40 GB of disk to start. Its size is your decision.
2. **Docker** on the server: follow <https://docs.docker.com/engine/install/> for your system,
   then check:
   ```
   docker compose version
   ```
3. **A domain** you control, added to your Cloudflare account (for example `read.YOURDOMAIN`).
4. **A Cloudflare tunnel**: Cloudflare dashboard > Zero Trust > Networks > Tunnels > Create a
   tunnel > Cloudflared. Name it `mothli`. Copy the **token** it shows (the long text after
   `--token`). Under "Public hostname", add your domain with service **HTTP** and URL
   **`proxy:8080`**.
5. **Cloudflare settings** for the domain:
   - SSL/TLS > Overview > Configure: **Full (strict)**.
   - SSL/TLS > Edge Certificates: **Always Use HTTPS** on.
   - Caching > Cache Rules: one rule with the expression
     `(starts_with(http.request.uri.path, "/api/"))`, action **Bypass cache**.
   - Security > Security rules > Rate limiting rules: one rule for the two sign-in addresses
     only, with the expression
     `(http.request.uri.path eq "/api/auth/login") or (http.request.uri.path eq "/api/auth/callback")`,
     5 requests per 10 seconds per IP address, action **Block** for 10 seconds. (The free plan
     offers only a 10 second window and one rule.) Not all of `/api/auth/`: the app asks
     `/api/auth/session` on every page load, so a limit there could block a household or an
     office whose readers share one address.
6. **The sign-in service** (any OpenID Connect provider). Create a "web application" there with:
   - redirect (callback) address `https://YOURDOMAIN/api/auth/callback`
   - scopes `openid profile email`
   - Note its **issuer** address, **client ID** and **client secret**. Copy the issuer exactly
     as the service shows it, including a slash at the end if it has one.
   - **If the service is Auth0:** choose the application type **Regular Web Application**. The
     issuer is `https://<your tenant domain>/`, with the slash at the end (for example
     `https://your-tenant.us.auth0.com/`). In the application's settings, set the token
     endpoint authentication method to **Basic**: Mothli sends the client secret that way,
     and sign-in will not work with any other choice.
7. **The two AI accounts, for Mothli's chat** (optional): the **writer**, which writes Mothli's
   replies (an OpenAI key, or an Anthropic key instead), and the **checker**, Jev, run by
   TypeSafe, which checks every sentence against what the reader has read before it is shown (a
   TypeSafe key). You pay for both. Mothli runs without them: readers read, keep their place,
   highlights, notes and bookmarks, and use the free library, and the app shows no chat at all.

## 2. First deployment

`deploy/setup.sh` writes the two settings files for you: it makes every password and key, writes
the values you give it, and never shows a password or a key on the screen. You can run it again
at any time: it never changes a value that is already there.

1. Get the code (the folder can be anywhere; `~/mothli` is only the example used here):
   ```
   git clone https://github.com/jouleka/mothli.git ~/mothli
   cd ~/mothli
   ```
2. Make the two settings files, `deploy/.env` and `deploy/app.env`. Put your own values in place
   of the words in capitals:
   ```
   deploy/setup.sh --domain YOURDOMAIN --oidc-issuer ISSUER --oidc-client-id CLIENT_ID
   ```
   It makes both files readable by you alone, makes the nine database passwords and the key that
   encrypts every book file (`HOSTED_STORAGE_FILESYSTEM_KEY`), and writes your domain and the
   sign-in service's issuer and client ID (step 1.6). Leave out a value you do not have yet: it
   asks for each one it is missing (press Enter to skip one). `deploy/setup.sh --help` lists the
   values it can take later: who runs Mothli (step 4d), Paddle's ids (step 4c) and the R2 address
   (step 4.1).
3. Paste the two values only you have. Copy the value first, then run the command and paste.
   Each command asks for one value and hides what you paste:
   ```
   deploy/setup.sh set CLOUDFLARE_TUNNEL_TOKEN
   deploy/setup.sh set OIDC_CLIENT_SECRET
   ```
   The token is the one from step 1.4 (copying Cloudflare's whole install command is fine: the
   token is kept from it); the client secret is from your sign-in service. Each command answers
   `... looks right`, or writes nothing and says what kind of value it expected. To change a value
   later, run the same command: it asks before it replaces anything.

   **For Mothli's chat**, paste the two AI keys the same way, the writer's last:
   ```
   deploy/setup.sh set TYPESAFE_API_KEY
   deploy/setup.sh set OPENAI_API_KEY
   ```
   (or `deploy/setup.sh set ANTHROPIC_API_KEY` in place of the OpenAI key). Everything else the
   chat needs is already set. Then turn on the job worker, which writes Mothli's notes on
   finished chapters, what it remembers about readers and its notes on every book's story (step
   4b):
   ```
   deploy/setup.sh worker on
   ```
   It refuses, saying why, until both keys are in; `deploy/setup.sh worker off` turns it off
   again. Leave both keys empty and the server starts without a chat (and without the worker line).
   Fill in only one and the web process stops at startup with a line naming the missing one
   (step 7 below).
4. Check what is still missing:
   ```
   deploy/setup.sh check
   ```
   It lists settings by name only, never a value, in two lists, each with what to do. The first
   list, "Still needed before Mothli can start", must be gone: then it says
   `Ready to start: nothing needed to start is missing.` The second list, "Needed later", is for
   the backups, Paddle and the chat, in steps 4 and 4c.
5. **Copy both files somewhere safe and offline now** (a password manager or an encrypted USB
   stick). Without `HOSTED_STORAGE_FILESYSTEM_KEY` no book can ever be read again, backups
   included.
6. Build and start:
   ```
   docker compose -f deploy/compose.yaml up -d --build --wait
   ```
   The first build takes several minutes. `--wait` returns when everything is healthy. This
   starts only Mothli's own containers (the project is called `mothli`); nothing else on the
   server is touched.
7. Put Mothli's three scheduled jobs in the server's crontab: the nightly backup at 03:00 (step
   4), the daily erasing of deleted accounts at 04:00 (step 4a) and the hourly check of plans with
   Paddle at :30 (step 4c):
   ```
   deploy/setup.sh cron
   ```
   It adds three lines, each marked `# mothli-setup:...`, with this folder's real path, and leaves
   every other line of the crontab alone. Running it again replaces those three and nothing else.
   `deploy/setup.sh cron --show` prints them without changing anything; `crontab -l` shows the
   whole crontab. Until step 4.1 is done, `deploy/backups/backup.log` says
   `BACKUP FAILED while checking the backup key` every night, and until Paddle's API key is in
   (step 4c), `deploy/backups/paddle-check.log` says `PADDLE_API_KEY and HOSTED_BILLING_DSN are
   required` every hour. Both are expected then and change nothing. Each line runs its job
   through `deploy/report.sh`, which leaves the job and its log as they are and, once the alerts
   are set up (step 6a, after the first backup works), emails you when a job fails or goes quiet.
8. Check it (step 6 below), then open `https://YOURDOMAIN` and sign in.

**By hand, if you ever need to** (to repair a file, say): every password is
`openssl rand -hex 32`, the book files' key is `openssl rand -base64 32`, and both files are
edited with `nano deploy/.env` and `nano deploy/app.env` (then `chmod 600 deploy/.env
deploy/app.env`). Never make a new `HOSTED_STORAGE_FILESYSTEM_KEY` for a server that has books:
take it from your offline copy.

## 3. Updating

```
cd ~/mothli
git pull
docker compose -f deploy/compose.yaml up -d --build --wait
```

The `migrate` step runs first on every start: it updates the database and resets every
database permission to exactly what the code expects. If it fails, the new web process does not
start; see "When something is wrong".

An update can need a new line in the settings files. After `git pull`, and before `up`, run:

```
deploy/setup.sh
deploy/setup.sh check
```

The first adds any new password the update needs (the update that brought Paddle's messages
needed `MOTHLI_DB_BILLING_PASSWORD`, for example) and changes nothing else; the second names
anything left for you. Without a needed line Compose stops with `set ... in deploy/.env` and
nothing changes.

## 4. Backups (every night, to Cloudflare R2)

Every night at 03:00 the server makes one backup file and puts it in a private Cloudflare R2
bucket, `mothli-protected-backups`. The newest 14 are kept there; older ones are removed, but only
after the new one has arrived whole. The app keeps running while it happens.

**What is in a backup:** the whole database (accounts, everything readers keep in their books,
their conversations with Mothli, Mothli's chapter notes and story memory, spend records, the
audit log) and the readers' book files. Story memory is in it on purpose: making it again would
cost about $2.60 for every 30 chapters.

**What is not, on purpose:** the two settings files `deploy/.env` and `deploy/app.env` (they hold
the keys; keep your offline copy from step 2.5), the database passwords and roles (made again from
`deploy/.env` when you restore), the app itself (it is in git) and the logs.

**Every backup is encrypted on the server before it leaves it**, with the backup key,
`MOTHLI_BACKUP_ENCRYPTION_KEY` in `deploy/.env` (you make it once, in step 4.1). What sits in R2
is unreadable without that key, and the key is never sent to R2 and never in git: anyone who gets
the R2 token, or into your Cloudflare account, gets files they cannot open. Inside the backup the
book files are encrypted a second time, as on the server, with `HOSTED_STORAGE_FILESYSTEM_KEY`
from `deploy/app.env`.

**So two keys open a backup, and both are in your offline copy of the settings files** (step 2.5):
the backup key in `deploy/.env` and the book files' key in `deploy/app.env`. **If you lose the
backup key, every backup is lost with it**: nobody, not Cloudflare and not whoever looks after the
code, can open them. There is nothing new to keep somewhere else: keep your offline copy of
`deploy/.env` up to date (step 4.1 tells you when) and it holds the key. Never make a new backup key
over the old one: the new one cannot open the backups made before it.

### 4.1 Once: the bucket and its key, in Cloudflare

1. Cloudflare dashboard > **R2 Object Storage** > **Create bucket**. Name it
   `mothli-protected-backups`, leave the location on Automatic, and create it. Leave "Public
   access" off (it is off unless you turn it on).
2. Back on the R2 page: **Manage API tokens** (the "API" button) > **Create Account API token**.
   - Name: `mothli-backups`
   - Permissions: **Object Read & Write**
   - Specify bucket(s): **Apply to specific buckets only** > `mothli-protected-backups`
   - TTL: Forever
   Then **Create Account API Token**.
3. Cloudflare now shows the token's values **once**. Keep that page open, and on the server paste
   each one (each command asks for its value and hides what you paste):
   ```
   deploy/setup.sh set MOTHLI_BACKUP_ACCESS_KEY_ID
   deploy/setup.sh set MOTHLI_BACKUP_SECRET_ACCESS_KEY
   deploy/setup.sh set MOTHLI_BACKUP_S3_ENDPOINT
   ```
   - `MOTHLI_BACKUP_ACCESS_KEY_ID`: the **Access Key ID**;
   - `MOTHLI_BACKUP_SECRET_ACCESS_KEY`: the **Secret Access Key**;
   - `MOTHLI_BACKUP_S3_ENDPOINT`: the address under **Use jurisdiction-specific endpoints for S3
     clients** (it looks like `https://` + a long code + `.r2.cloudflarestorage.com`). If you gave
     it to `deploy/setup.sh --r2-endpoint` in step 2, it says it is already there.

   The first value shown, "Token value", is not needed.
4. Check that the server can reach the bucket:
   ```
   docker compose -f deploy/compose.yaml run --rm backup
   ```
   It ends with `0 backups in mothli-protected-backups`. A line starting `backup failed:` names
   what is wrong (`... is not set in deploy/.env` means a value is missing).
5. Make the backup key (once, ever). It is made by Mothli's own image, so the images must be
   built (step 2.6):
   ```
   deploy/setup.sh backup-key
   ```
   It writes the key into `deploy/.env` without showing it, then checks it: the last lines say
   `the backup key in deploy/.env is set`. If it says `... is already set: kept`, you made one
   before: keep it and do not make another.

   By hand, only to repair a file: `docker compose -f deploy/compose.yaml run --rm backup python
   -m app.hosted.backups new-key` prints a new key (a line starting `AGE-SECRET-KEY-1`); paste it
   with `deploy/setup.sh set MOTHLI_BACKUP_ENCRYPTION_KEY`, then `clear` the screen.
6. **Copy `deploy/.env` to your offline place again now**, over the old copy: it holds the backup
   key now, and without the key no backup can ever be opened.
7. Make the first backup by hand:
   ```
   cd ~/mothli
   deploy/backup.sh
   ```
   The last line says `backup ok: mothli-backup-<date and time>.tar.age, ... MB, in ... s`.
8. It runs every night at 03:00 (before the 04:00 erasing of step 4a) from the crontab line that
   `deploy/setup.sh cron` added in step 2.7. If you skipped that step, run it now.

### 4.2 Did last night's backup run?

```
tail -n 4 deploy/backups/backup.log
docker compose -f deploy/compose.yaml run --rm backup
```

The log's last line should be `backup ok: ...` with last night's date; the second command lists
what is in the bucket, newest first, with sizes. A failed or missing backup also emails you (step
6a); look here once a month anyway. A line `report.sh: could not tell the watcher ...` after it
is about the alerts only: the backup itself is the line above.

If the log's last backup line starts `BACKUP FAILED while ...`, the backup did not happen and **nothing
in R2 was removed**. The words after `while` say which step failed, and the line above it often
says why. Run `deploy/backup.sh` by hand to see it again. Usual causes: the backup key is missing
or damaged (`BACKUP FAILED while checking the backup key`, with
`MOTHLI_BACKUP_ENCRYPTION_KEY ... is not set` or `... is damaged` above it: nothing was dumped;
copy the line back from your offline copy of `deploy/.env`, and make a new key with step 4.1
point 5 only if there never was one), the R2 lines in
`deploy/.env` are wrong or the token was deleted (`backup failed: ... talking to the bucket`), the
server's disk is full (`df -h`), or Mothli is not running
(`docker compose -f deploy/compose.yaml ps`). If the upload failed, the backup file is kept in
`deploy/backups/` and removed by the next backup that works.

### 4.3 Erased accounts stay in the backups for up to 14 days

When a reader's account is erased (next section), it is gone from the server, but the backups made
before that day still hold it, books included, until they are removed: at most 14 days later. So
a reader's data really disappears 14 days after it is erased. **The privacy page has to say
this.** If you ever restore an older backup, run the erasing step right after it (step 5.2).

## 4a. Erasing deleted accounts (every day)

When a reader deletes their account, Mothli signs them out everywhere at once and keeps
everything for 30 days in case they change their mind (signing in again lets them keep it).
After that a daily step erases it: their book files, then every row of theirs. What is left is
the account's bare id and a few audit lines saying when it was asked for and when it was erased,
with no name, no email and no content.

It runs every day at 04:00 from the crontab line that `deploy/setup.sh cron` added in step 2.7.
It uses its own database role (`MOTHLI_DB_PURGE_PASSWORD` in `deploy/.env`), which only this step
has. Run it by hand the same way:

```
docker compose -f deploy/compose.yaml run --rm purge
```

The same step erases Mothli's notes on a book file nobody has any more: 30 days after the last
reader with that exact file removed it or deleted their account (ADR 0044 section 6). A reader
who brings the file back within the 30 days keeps the notes; one who brings it back later gets
them built again, paid again.

**Checking it ran.** Each run prints one line, for example
`erased 1, unfinished 0, held 0, waiting 3; editions erased 0, unfinished 0, waiting 2`: accounts
erased in this run, accounts it could not finish (it ends with an error then), accounts held back
because their Paddle plan still renews (next part; it ends with an error then too), and accounts
still inside their 30 days; then the same for book files' notes. `tail deploy/backups/purge.log` shows the last runs
(a line `report.sh: could not tell the watcher ...` is about the alerts only, step 6a). An
account it could not finish (book storage unreachable, the database restarted) is finished by the
next run; nothing of it is shown to anyone meanwhile. The audit log of an erased account ends with
`account.purge`:

```
docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.audit show --owner READER_ID
```

### Deleting an account while paying

A reader who deletes their account while on the plan is never charged again (ADR 0045 step 7).
Asking to delete records, in their plan's row, that the cancel is owed to them (the column
`cancel_owed_since`), then asks Paddle to cancel the plan at the end of the period already paid
for, exactly as "Cancel my plan" does. If Paddle is down, slow or refuses just then (it refuses a
cancel in the 30 minutes before a renewal), the deletion still goes through, and the regular
check (part 4c, hourly) asks again every run, those plans first, until Paddle's answer shows the
cancel scheduled. Nothing for you to do in the ordinary case. The waiting screen tells the reader
"Your plan won't renew." with the day it is paid for until.

- **Changing their mind.** Within the 30 days the reader can keep their account; their plan still
  ends on its day unless they press **Keep my plan** (on the waiting screen or in Your account),
  which removes the cancel. If the paid period ends first, they are like any reader whose plan
  ended.
- **The purge never erases an account whose plan still renews.** If, when the 30 days are over,
  the plan is still live with no scheduled end (Paddle never confirmed the cancel), the purge holds
  that account, untouched, and writes to `purge.log`:
  `purge held an account whose Paddle plan still renews: ...`. The line names no one. The run then
  ends with an error, and `held` is above 0. The regular check keeps asking Paddle; the next purge
  after Paddle confirms erases the account. If `held` stays above 0 for more than a day, look at
  `paddle-check.log` (`failed` above 0 means Paddle is refusing or down), and find the held
  accounts and their subscriptions:

  ```
  docker compose -f deploy/compose.yaml exec db psql -U postgres -d mothli -c "SELECT u.id, p.paddle_subscription_id, p.paddle_status, p.cancel_owed_since FROM users u JOIN reader_plans p ON p.owner_id = u.id WHERE u.status = 'deleting' AND u.erase_after <= now() AND p.paddle_status IN ('active','trialing','past_due') AND p.ends_at IS NULL"
  ```

  Then cancel each subscription in Paddle's dashboard (**Customers > Subscriptions**, the
  subscription id, **Cancel**, at the end of the billing period); the next regular check and purge
  finish the rest.
- **A renewal that came first.** If Paddle refused the cancel because a renewal was under way, the
  web log or `paddle-check.log` says `paddle refused an owed cancel while a renewal is under way:
  ... refund it (runbook)`. That reader may have been charged once more after asking to delete.
  Find readers whose cancel is owed with the query above, without the `erase_after` condition (or
  `cancel_owed_since IS NOT NULL` alone), and in Paddle refund any payment of theirs made after
  their `cancel_owed_since`.
- **What is erased, and what is not.** Erasure takes the plan's row (`reader_plans`), the record of
  Paddle's messages about it (`paddle_events`) and the reader's checkouts (`paddle_checkouts`)
  with everything else. Paddle is the seller and keeps its own records of the customer, the
  subscription and the payments; Mothli asks Paddle to delete nothing. The privacy page says so
  (its sentence is in `docs/product/companion-open-items.md`).
- **The copy of their data** holds `your plan.json`: any gift, and their plan's kind, status and
  dates. No Paddle id, token or amount.

## 4b. Story memory: who's who and the story so far

Mothli keeps notes on every reader's book, uploaded or from the free library: who's who and the
story so far. They are made once for each exact book file, a chapter at a time, as readers reach
the chapters (one chapter ahead of the reader furthest on), and each reader is handed them only
up to where they are (ADR 0044). There is nothing to run and no title to choose: it is on in
`deploy/app.env` (`HOSTED_STORY_MEMORY=true`, as the template sets it) and is done by the job
worker, so the worker must be on (`COMPOSE_PROFILES=worker` in `deploy/.env`, step 2.3). With no
AI keys it is off, like the chat. Keep it on: with it off the story panels keep telling readers
"No one yet. Keep reading and I'll keep track of who's who.", which would never come true.

**What it costs, and who pays.** Mothli pays, never a reader: about $0.09 a chapter at today's
prices (about $7 for a whole novel), once per book file however many readers have it. It is
spent from the notes account (a reserved account the database makes for itself; nobody signs in
to it), at most `HOSTED_STORY_MEMORY_USD_PER_DAY` a day ($10 unless you set it), and that counts
toward the platform's daily cap, `HOSTED_PLATFORM_USD_PER_DAY` ($15). A chapter that does not fit
waits for the next day; readers keep reading meanwhile.

**Guards against abuse**, which a real reader should never meet: one reader's reading may be the
first to have at most 30 new chapters made a day, 300 a month, and 5 new book files a month
(`HOSTED_STORY_MEMORY_NEW_CHAPTERS_PER_DAY`, `HOSTED_STORY_MEMORY_NEW_CHAPTERS_PER_MONTH`,
`HOSTED_STORY_MEMORY_NEW_FILES_PER_MONTH`). Chapters someone else's reading already made never
count. All five settings are in the caps section of `deploy/app.env`, commented out at their
defaults; raise one if a real reader ever reaches it.

**Each reader's own background work.** Beside the shared story memory, the worker writes each
reader's own chapter notes and what Mothli remembers of them. That is Mothli's cost too, never the
reader's allowance: a reader's day and month count only their conversation (ADR 0041 section 6,
amended 20 November 2026). It is still in the reader's ledger and counts toward the platform's
daily cap. It is held by a day budget for all readers together, `HOSTED_BACKGROUND_USD_PER_DAY`
($3), and by guards against abuse per reader, `HOSTED_READER_BACKGROUND_USD_PER_DAY` ($1) and
`HOSTED_READER_BACKGROUND_USD_PER_MONTH` ($5); a reader who marks 60 chapters read on their first
day uses about $0.42. Past any of them the work waits for the next day and the reader is told
nothing; their chat goes on. Note that the platform's day ($15) must leave room for the chat
beside story memory ($10) and this budget ($3).

**Background work by plan** (ADR 0045 step 6, ADR 0046). Both kinds above run as described for
paying readers (gifts included). For a reader whose plan or gift has stopped, none: the worker
finishes their waiting jobs without doing or paying for anything, and their pages ask for no new
ones (their place is still kept). When they pay again, their next question asks for it, and it
catches up to where they are. A new reader (never a plan or a gift) gets a warm-up, so their
first session is not cold: within 30 days of signing up (`HOSTED_WARMUP_DAYS`), up to 25 chapters
of story memory their reading is the first to have made (`HOSTED_WARMUP_NEW_CHAPTERS`) and $0.50
of chapter notes (`HOSTED_WARMUP_NOTES_USD`); past any of the three, nothing until they pay. At
most about $2.75 a sign-up who never pays. Readers are never told any of it. The worker reads a reader's plan, and when they
signed up (two columns of `users`, never a name or an email); `scripts/hosted_roles.py deploy`
grants and checks both.

## 4c. Paddle's messages: how a reader becomes a paying one

Paddle tells Mothli about each subscription by calling `https://YOURDOMAIN/api/billing/paddle`
with a signed message (ADR 0045 step 3). Mothli checks the signature, applies the subscription's
status, and answers. Until you set it up, the address does not exist and nothing about plans
changes. Subscribing, cancelling and updating a card go through Paddle's API with its own key
(**The API key**, below); the app's screens for them come in a later step.

**Your Paddle account also sells MySigner.** Its messages arrive here too. Mothli takes a message
as its own only when it is about a subscription and every item on it is one of the price ids you
put in `PADDLE_PRICE_MONTHLY` and `PADDLE_PRICE_YEARLY`. Every other message is answered "OK" and
not kept at all. So those two lines must hold Mothli's prices and never MySigner's.

To set it up, first in Paddle's sandbox (its own dashboard, its own ids and secret), later the
same in the live account:

1. In Paddle: **Catalog > Products**, Mothli's product with its two prices ($12.99 a month,
   $79.99 a year). Copy each price's id (`pri_...`).
2. In Paddle: **Events > Notifications > New destination**. Notification type: webhook. URL:
   `https://YOURDOMAIN/api/billing/paddle`. Events: the `subscription.*` ones
   (`subscription.created`, `.updated`, `.activated`, `.canceled`, `.past_due`, `.paused`,
   `.resumed`, `.trialing`), and the two adjustment ones, `adjustment.created` and
   `adjustment.updated` (refunds and chargebacks; **Refunds and chargebacks**, below). Mothli reads
   no other kind; any other is answered and ignored.
   Usage type: real and simulated events while you test (platform only afterwards). Save, then
   copy the destination's secret key (`pdl_ntfset_...`) and keep it like a password; Paddle may
   not show it again.
3. On the server, write which Paddle and the two price ids from step 1, then paste the
   destination's secret key from step 2:
   ```
   deploy/setup.sh --paddle-environment sandbox --paddle-price-monthly MONTHLY_ID --paddle-price-yearly YEARLY_ID
   deploy/setup.sh set PADDLE_WEBHOOK_SECRET
   ```
   (`production` in place of `sandbox` for the live account.) Then
   `docker compose -f deploy/compose.yaml up -d --wait`.

   **Moving from the sandbox to live** changes values that are already there, so use `set` for
   each one, which asks before it replaces: `deploy/setup.sh set PADDLE_ENVIRONMENT` (type
   `production`), then `PADDLE_PRICE_MONTHLY`, `PADDLE_PRICE_YEARLY`, `PADDLE_WEBHOOK_SECRET`,
   `PADDLE_API_KEY` and `PADDLE_CLIENT_TOKEN` the same way.
4. In Paddle, send a simulated `subscription.created` to the destination (Paddle's webhook
   simulator). Its sample is for a price that is not Mothli's, so Mothli answers OK and keeps
   nothing: Paddle shows the delivery as successful. A delivery Paddle shows as failed with 400
   means the secret does not match: copy it again.

**A new price later** (the yearly launch price becoming $89.99, a new price in Paddle): put the
new id first and keep the old one, separated by a comma, with `deploy/setup.sh set
PADDLE_PRICE_YEARLY` (for example `pri_new...,pri_old...`), then `up -d --wait`. Readers on the old price stay
paying for as long as it is in the line.

**Where a reader stands.** `python -m app.hosted.plans show ACCOUNT_ID` (step 7) shows `paying`
with `paid_by` `paddle` while their plan is live: active, or a payment Paddle is retrying
(`past_due`), or cancelled but paid to the end of the period. A paused or cancelled plan, or one
ended by a refund or a chargeback, shows `ended` with `why` `plan`. A gift wins while it lasts.

**Refunds and chargebacks.** Paddle does not cancel a subscription when you refund one of its
payments, and it does not cancel one after a chargeback either: it only sends an adjustment
message. So Mothli does it. When an **approved refund of a whole payment** (in Paddle: refund the
transaction in full) or a **chargeback** reaches Mothli for a subscription Mothli stored for one
of its readers, that reader's plan ends at once (they see the usual "Your plan has ended"
invitation, and may subscribe again), and Mothli cancels the subscription at Paddle **at once**
(not at the end of the period), so it never renews. Paddle's own `subscription.canceled` follows.
What does not end the plan:

- a **partial refund** (some of the money back, or some items): the plan goes on. Use one when you
  want to give money back as a kindness and keep the reader on their plan;
- a refund **still waiting for Paddle's approval** (most live refunds over a limit wait for Paddle's
  review): nothing happens until Paddle approves it (`adjustment.updated`); a rejected one changes
  nothing;
- a credit, a chargeback warning, or a chargeback Paddle won back (the plan a chargeback ended
  stays ended: a cancelled subscription cannot be restarted; the reader may subscribe again).

An adjustment names no price, so Mothli takes one as its own only when its subscription and
customer are exactly ones it stored for a reader; MySigner's refunds and chargebacks are answered
"OK" and kept nowhere, and Mothli never calls Paddle about them. If Paddle is unreachable when the
refund arrives, the plan still ends; the regular check (below) asks Paddle for the cancel again
every hour until Paddle says cancelled, and a reader who tries to subscribe again meanwhile is
asked to try again in a minute. The logs may then say `a refunded plan could not be cancelled at
Paddle` or `paddle refused to cancel a refunded plan ...`; if one says `... while a renewal is
under way ... refund it (runbook)`, that reader may have been charged once more: find them with

```
docker compose -f deploy/compose.yaml exec db psql -U postgres -d mothli -c "SELECT owner_id, paddle_subscription_id, paddle_status, refunded_at FROM reader_plans WHERE refunded_at IS NOT NULL AND paddle_status <> 'canceled'"
```

and in Paddle cancel that subscription (**Customers > Subscriptions**, **Cancel**, immediately)
and refund any payment of theirs made after `refunded_at`. One limit: a refund that arrives before
Mothli has stored the subscription at all (its very first message still on its way) is not
recognised as Mothli's; refund only after the reader shows as paying, or cancel the subscription
in Paddle yourself.

**Adding the refund events to a destination made before** (once, in the sandbox and in the live
account): in Paddle, **Events > Notifications** (as in step 2), open Mothli's destination (the one whose
URL is `https://YOURDOMAIN/api/billing/paddle`), and tick **`adjustment.created`** and
**`adjustment.updated`** beside the `subscription.*` events already ticked; save. Leave every
other event as it is (Paddle unsubscribes any event left unticked when a destination is saved),
and do not touch MySigner's destination. Nothing changes on the server, and the API key needs no
new permission (an immediate cancel is **Subscriptions: write**, which it has).

**Rotating the secret.** Paddle may sign with an old and a new secret for a while; Mothli accepts
a message that matches the secret it has. Paste the new secret with `deploy/setup.sh set
PADDLE_WEBHOOK_SECRET` and `up -d --wait` as soon as Paddle shows it.

The web log says only when a message was refused (`paddle message refused: signature` or
`... malformed`), never a secret, an email address or an id. What was done with each of Mothli's
messages (`applied`, `older`, `other_subscription`, `no_account`, and for an adjustment that ends
nothing, `unchanged`) is kept with its id, type and
time in the database, never its contents. A message about a new subscription is applied only when
it came from a checkout Mothli itself made for that reader (ADR 0045 step 4); one that names a
reader without that is kept as `no_account` and changes nothing.

**The API key** (ADR 0045 step 4). Mothli calls Paddle's API to start a checkout, cancel at the
end of the period, undo that cancel, open the customer portal ("Update payment details") and run
the regular check below. Make a key of its own for Mothli, first in the sandbox, later live:

1. In Paddle: **Developer tools > Authentication > API keys > New API key**. Name it "Mothli
   server". Give it exactly these permissions, nothing else: **Transactions: write** (a checkout
   is a transaction Mothli makes), **Subscriptions: write** (read, cancel, cancel at once after a
   refund, undo a cancel; write includes read), **Customer portal sessions: write**, **Prices: read** (the front page shows
   the two prices as your catalog has them; step 4d). Mothli needs nothing on customers,
   products, notifications or anything else, and makes no customer of its own. Choose an
   expiry (Paddle's default is 90 days, a year at most) and, if offered, make it rotatable.
2. Copy the key (Paddle shows it once; a sandbox key starts `pdl_sdbx_apikey_`, a live one
   `pdl_live_apikey_`) and keep it like a password.
3. Paste it with `deploy/setup.sh set PADDLE_API_KEY`. It must come from the same Paddle as
   `PADDLE_ENVIRONMENT` (the command says so if it does not, and the server refuses to start) and
   needs `PADDLE_WEBHOOK_SECRET` set: a payment is only ever applied from Paddle's message. The
   same command turns on `PADDLE_CHECKOUT_URL`, Mothli's own front page `https://YOURDOMAIN/`,
   which `deploy/setup.sh` made from your domain and kept off until the key was in (the server
   refuses one without the other; **One Paddle account, several products**, below, says why).
   The server also refuses the key without the three `OPERATOR_` lines of step 4d (who runs
   Mothli, printed on its terms). Run `deploy/setup.sh check`, then
   `docker compose -f deploy/compose.yaml up -d --wait`.
4. Put the key's expiry date in your calendar, a week early. An expired key cannot be renewed:
   make a new one the same way, paste it with `deploy/setup.sh set PADDLE_API_KEY` (it asks
   before replacing the old one), `up -d --wait`, then revoke the old one in Paddle.
   Until then, subscribing, cancelling and the portal answer "I can't reach our payment service
   just now".

**Your Paddle account also sells MySigner, and Paddle's permissions are per kind of thing, not
per product**: this key could reach MySigner's transactions and subscriptions too. Mothli's code
only ever names a subscription or customer it stored for the signed-in reader, or makes a
transaction for one of its own price ids; it never lists or searches anything. Keep the key on
the server only, and never give MySigner's code this key or Mothli's code MySigner's.

The web log says only `paddle call failed: timeout` (or `unreachable`, `server`, `unreadable`)
or `paddle call refused`, never the key, an id, an address or what Paddle answered. Readers then
see a kind line and nothing changes.

**The regular check.** Paddle recommends checking plans against its API now and then, so that a
message lost, refused or out of order is repaired without waiting for the next renewal. Mothli
fetches, one at a time with a pause between, up to 100 of the subscriptions it stored that are
live or changed in the last 30 days, and applies each as a message would (newer only, Mothli's
prices only). It never lists anything in Paddle. It runs every hour at :30 from the crontab line
that `deploy/setup.sh cron` added in step 2.7 (before the API key is in, each run only says
`PADDLE_API_KEY and HOSTED_BILLING_DSN are required`). By hand:
`docker compose -f deploy/compose.yaml exec -T web python -m app.hosted.paddle_check`.

It runs in the web container, which already holds the key, the billing role's address and the
prices. Each run prints one line, `checked 12, changed 0, failed 0`; `failed` above 0 means Paddle
was unreachable or refused (the run stops at the first sign Paddle is down and tries again next
hour). `tail -n 5 deploy/backups/paddle-check.log` shows the last runs (a line
`report.sh: could not tell the watcher ...` is about the alerts only, step 6a). `--size` (1 to
1000) and `--pause` (seconds, 0.25 to 60) change the page and the pause.
It is also what guarantees the cancel owed to a reader who deleted their account while paying
(part 4a, "Deleting an account while paying"): those plans come first, and each that Paddle still
shows renewing is asked for its cancel, counted in `changed` when Paddle confirms. And the same
for a plan a refund or a chargeback ended: while Paddle does not yet say cancelled, it is asked
for the cancel at once (**Refunds and chargebacks**, above).

**The client token** (ADR 0045 step 4, the plan sheet). The reader's browser uses it to show the
two prices from Paddle and open Paddle's checkout. Paddle calls it safe to publish, but it is still
set here, never written in the app:

1. In Paddle: **Developer tools > Authentication > Client-side tokens > New client-side token**.
   Name it "Mothli web". A sandbox token starts `test_`, a live one `live_`.
2. Paste it with `deploy/setup.sh set PADDLE_CLIENT_TOKEN`. It must come from the same Paddle as
   `PADDLE_ENVIRONMENT` (the command says so if it does not, and the server refuses to start) and
   needs `PADDLE_API_KEY` set (the server makes every checkout). Then
   `docker compose -f deploy/compose.yaml up -d --wait`.
3. Live only: in Paddle's dashboard, under **Website approval**, add your domain. Paddle's live
   checkout opens only on an approved domain; the sandbox approves domains by itself
   ([go-live checklist](https://developer.paddle.com/build/go-live-checklist/)). The next part
   says more.

Without it, Your account still shows where each reader stands, with no "See the plan". The
browser's security policy allows Paddle's own addresses for its script, its checkout window and
its prices (`backend/app/main.py`, `deploy/Caddyfile`); nothing to set for that.

**One Paddle account, several products.** Mothli is sold from the same Paddle account as MySigner,
and that stays so. Paddle supports it ([one account, several
products](https://www.paddle.com/help/start/set-up-paddle/can-i-have-multiple-paddle-accounts)),
but some things exist once per account and belong to both products:

- the **statement descriptor** (what a buyer's bank statement says), the **display name** and the
  legal name;
- the **default payment link** (**Checkout > Checkout configuration**), which is MySigner's
  pricing page. (Paddle's help page names the first three; the default payment link is one
  setting of the account too.)

Do not change any of these for Mothli. Mothli does not need the default payment link: every
checkout Mothli makes carries its own page, `PADDLE_CHECKOUT_URL`, as the transaction's
`checkout.url`, which Paddle uses instead of the default for that checkout ([default payment
link](https://developer.paddle.com/build/transactions/default-payment-link)). Paddle's emails
about a checkout (the reminder after one left unpaid, for example) then bring the buyer to
Mothli's front page with `?_ptxn=...`, and Mothli opens that checkout there, signed in or not.

- **Approve Mothli's domain first.** In Paddle: **My account > Settings > Website approval**, add
  `mothli.com`, and every other host Mothli is served from (`www.mothli.com` if readers use it;
  the host in `PADDLE_CHECKOUT_URL` must be one of them), and wait until Paddle shows each as
  approved. Until then, Paddle refuses every live checkout: readers see "I can't reach our payment
  service just now, so nothing has changed", and the web log says `paddle refused the checkout
  address: approve the domain of PADDLE_CHECKOUT_URL in Paddle under Website approval`. The
  sandbox approves domains by itself (if one stays pending there, remove it and add it again).
- **One thing still goes to MySigner's page.** Paddle's own emails to a subscriber about updating
  their payment method link to the account's default payment link, not to the transaction's page
  (the default payment link page above: "The link to update a payment method redirects to your
  default payment URL"). Mothli cannot change that without changing the account's default, which
  it must not. Mothli's own screens send readers to **Your account > Update payment details**,
  which opens Paddle's customer portal for their own plan. If a Mothli reader writes in about a
  card update that landed on MySigner's page, point them there.
- **Each product has its own notification destination and secret.** Mothli's destination points
  at `https://YOURDOMAIN/api/billing/paddle` with its own secret in `PADDLE_WEBHOOK_SECRET`;
  MySigner keeps its own destination and secret. Never share one between them.
- **Each product's server receives the other's subscription messages** (Paddle sends every
  subscription event to every destination that asks for it) **and must ignore them.** Mothli does:
  it takes a message as its own only when every item is one of its price ids, and answers every
  other one "OK" without keeping it. MySigner's server must do the same with Mothli's.

## 4d. The public pages: who runs Mothli, the terms, privacy, refunds and contact

Anyone can read these without signing in, and Paddle's reviewers do before Paddle lets checkouts
work on your domain (ADR 0045 step 8). Paddle asks for a clear description of what you sell, its
prices, Terms and Conditions, a Refund Policy and a Privacy Policy reachable from the site's
navigation, and your legal name in the terms
([domain approval](https://www.paddle.com/help/start/account-verification/what-is-domain-approval)).
Mothli has them at these addresses, with a footer linking all four on every signed-out page:

- `https://YOURDOMAIN/` the front page: what Mothli is, what is free, the plan and its prices;
- `https://YOURDOMAIN/terms`, `/privacy`, `/refunds` and `/contact`.

**Your details.** The pages print who runs Mothli from three lines in `deploy/app.env`. Write them
with one command (the quotes keep a name with spaces together):

```
deploy/setup.sh --operator-name "YOUR LEGAL NAME" --operator-country "YOUR COUNTRY" --operator-email YOUR@ADDRESS
```

What each one is:

- `OPERATOR_LEGAL_NAME`: your legal name, or your company's, exactly as it should appear in the
  terms (Paddle's account holds the same legal name for both your products);
- `OPERATOR_COUNTRY`: the country you run Mothli from; the terms say its law applies;
- `OPERATOR_CONTACT_EMAIL`: the address readers write to. It is printed on every page, so use
  one you are happy to publish and that you read.

Then `docker compose -f deploy/compose.yaml up -d --wait`. Until all three are set, each page
says only that Mothli is not open for sign-ups here, and **the server refuses to start with
`PADDLE_API_KEY`** (a server that can take payments must publish its terms): it stops with
`PADDLE_API_KEY requires OPERATOR_LEGAL_NAME, OPERATOR_COUNTRY and OPERATOR_CONTACT_EMAIL`. A
value with markup or on more than one line, or the template's placeholder, is refused by name.

**The prices on the front page** come from your Paddle catalog, never from the app: the web
server reads the two prices it sells (the first id in `PADDLE_PRICE_MONTHLY` and in
`PADDLE_PRICE_YEARLY`) with the API key, which needs **Prices: read** (step 4c, the API key), and
keeps them for ten minutes. It asks for those two ids and nothing else in the shared account.
Where it cannot (no key, the key lacks that permission, Paddle unreachable, a price archived or
not monthly and yearly, the two in different currencies) the page shows the plan without figures
and asks again a minute later; nothing is logged but Paddle's usual one line. If you change a
price's amount in Paddle, the front page shows it within ten minutes.

**The words.** The terms, privacy notice and refunds page are in `frontend/src/public/legal.tsx`
and are true to what the code does on the day they are dated. Before going live, have them read
by someone who knows the law where you are: ADR 0045's "Step 8 built" lists each statement that is
a legal choice (the law that applies, the limit on responsibility, the minimum age of 16, who is
responsible for readers' data and on what basis, data handled outside the EU) and the ones you
must check yourself: name your sign-in service and the company you rent the server from, if you
want them named; and check what each AI company keeps of what it is sent, under your account's
terms with them. The privacy page names the AI company your chat uses by itself, from the AI keys
in `deploy/app.env` (Anthropic, OpenAI, or the host in `OPENAI_BASE_URL`), and with no keys says
the chat is not open here.

The proxy serves the app's page at those four addresses (`deploy/Caddyfile`, `@pages`); nothing
else changes in it, and the browser's security policy is the same as before.

## 5. Restoring

### 5.1 Test a backup (once a month, on the live server)

```
cd ~/mothli
deploy/restore.sh --test latest
```

It downloads the newest backup, decrypts it with the backup key in `deploy/.env`, checks its
checksums, restores it into a scratch database (`mothli_restore_test`) beside the live one, checks
that every table has the backup's row count and which migration the database is at, counts the
book files, then deletes the scratch database. Readers notice nothing, and the live database is
not touched. It needs free disk of about twice the backup's size (`df -h`). The last line says
`TEST RESTORE OK: ...`, which also proves your backup key opens your backups. Anything else ends
with `TEST RESTORE FAILED while ...`: keep that output and look at the backups before you need one.
`... while decrypting the backup` with `... does not open this backup` above it means the key in
`deploy/.env` is not the one the backup was made with; `... damaged or cut short` means the file
itself is broken (try the backup before it, by name). Either way nothing was restored.

To test an older one, name it instead of `latest` (the names come from
`docker compose -f deploy/compose.yaml run --rm backup`).

### 5.2 Restore for real

Only into an empty Mothli: a new server, or this one after removing everything. Restoring an
older backup loses everything readers did after it was made. You need the **same** `deploy/app.env`
the backup was made with (the book files' key) and the **same** `deploy/.env` (the backup key and
the R2 lines): your offline copies.

1. On a new server: steps 1.2 and 2.1, then put your offline copies of `deploy/.env` and
   `deploy/app.env` in `deploy/` (`chmod 600 deploy/.env deploy/app.env`). Do not run
   `deploy/setup.sh` before that: it would make new passwords and a new book files' key.
2. Only if this server already runs Mothli and you mean to replace everything on it:
   ```
   docker compose -f deploy/compose.yaml down --volumes
   ```
   This deletes the database and all book files on this server.
3. Build, then restore the newest backup:
   ```
   docker compose -f deploy/compose.yaml build
   deploy/restore.sh latest
   ```
   (or a backup's name instead of `latest`). It prints six numbered steps:
   1. downloads the backup, decrypts it with the backup key and checks its checksums;
   2. checks that the database and the book folder are empty (it refuses otherwise);
   3. makes the database roles from `deploy/.env` and restores the database in one go;
   4. puts the book files back;
   5. checks every table's row count against the backup's, and prints the migration the
      database is at beside this code's newest (`... at migration 0037; this code's newest is
      0037`). A backup made by older code is fine (the next step updates it); one made by newer
      code is refused: run `git pull` and start again;
   6. starts everything: the deployment step resets every permission and runs the app's own
      checks, and it waits until the web process answers "ready".

   The last line says `RESTORE OK: ...`. Anything else ends with `RESTORE FAILED while ...` or
   `RESTORE REFUSED: ...`, naming the step.
4. Check it: step 6 below (`curl https://YOURDOMAIN/api/health/ready` answers `{"status":"ready"}`),
   then open the site and sign in.
5. Erase again what was erased after the backup was made (the requests are in the restored
   database, so the same step finds them):
   ```
   docker compose -f deploy/compose.yaml run --rm purge
   ```
6. Put the scheduled jobs in this server's crontab: `deploy/setup.sh cron` (step 2.7).

### 5.3 Opening a backup by hand, without Mothli's code

You should never need this: `deploy/restore.sh` does it all. It is here so that your backups stay
yours even if Mothli's code were gone. A backup file (`mothli-backup-<date and time>.tar.age`) is a
standard **age** file (<https://age-encryption.org>; the format is age v1, encrypted to the X25519
key that `MOTHLI_BACKUP_ENCRYPTION_KEY` holds). Inside is an ordinary tar. On any Linux or Mac
computer:

1. Install the `age` tool: `sudo apt install age` (Ubuntu, Debian) or `brew install age` (Mac).
2. Get the backup file: from the server's `deploy/backups/` folder after a restore downloaded it,
   or from the Cloudflare dashboard (R2 > `mothli-protected-backups` > the file > Download).
3. Put the backup key in a file only you can read, from your offline copy of `deploy/.env`:
   ```
   (umask 077; grep '^MOTHLI_BACKUP_ENCRYPTION_KEY=' deploy/.env | cut -d= -f2- > backup-key.txt)
   ```
4. Decrypt and unpack it, then check its checksums:
   ```
   age --decrypt -i backup-key.txt -o backup.tar mothli-backup-DATE.tar.age
   mkdir backup && tar -C backup -xf backup.tar
   (cd backup && sha256sum -c SHA256SUMS)
   rm backup-key.txt backup.tar
   ```
   If the key is wrong, or the file was changed or cut short, `age` stops with an error: then
   stop too, and delete whatever it wrote (`rm -f backup.tar`). The `sha256sum` line prints `OK`
   for each of `database.dump`, `counts.txt` and `books.tar`.
5. The folder `backup` now holds the database (`database.dump`, for PostgreSQL's `pg_restore`),
   the row counts and the book files (`books.tar`, still encrypted with
   `HOSTED_STORAGE_FILESYSTEM_KEY`). On a Mothli server, `deploy/restore.sh` takes that folder's
   full path (for example `/root/backup`) in place of `latest`: it checks the checksums again and
   restores from it as from a download.
   The folder holds readers' emails and conversations: delete it (`rm -rf backup`) when done.

`deploy/restore.sh` takes only an encrypted backup file or such an unpacked folder: it refuses a
plain `.tar`, since Mothli never makes an unencrypted backup.

## 6. Is it healthy?

```
docker compose -f deploy/compose.yaml ps
```

Every service should say `running (healthy)`, and `migrate` `exited (0)`. From any computer:

```
curl https://YOURDOMAIN/api/health/live
curl https://YOURDOMAIN/api/health/ready
```

`live` answers `{"status":"ok"}` while the web process runs. `ready` answers `{"status":"ready"}`
only when the database answers, its permissions are exactly as reviewed, and its version is the
code's; otherwise `{"status":"not_ready"}` with code 503. Point an uptime monitor at `ready`.

Recent messages from a service:

```
docker compose -f deploy/compose.yaml logs --tail 100 web
```

## 6a. Alerts: how Mothli tells you something is wrong

A small program in your Cloudflare account, the watcher (`deploy/watch/`), emails you through your
domain's Email Routing when:

- **the site is down**: every 5 minutes it asks `https://YOURDOMAIN/api/health/ready`; after two
  bad answers in a row (not `ready`, or nothing within 10 seconds) you get one email, and one more
  when it is back;
- **a scheduled job failed**: the backup (03:00), the erasing of deleted accounts (04:00) or the
  hourly Paddle check tell it how they went; a failure emails you at once, again at most every 6
  hours while it keeps failing, and once when it works again;
- **a job went quiet**: no word from the backup or the erasing for 26 hours, or from the Paddle
  check for 3 hours: once a day until it reports again (the job did not run, or the server cannot
  reach the watcher).

Each email says what happened and when, the first command to type, and the step of this runbook
to read. It is quiet until it has something to watch: the site until it has answered `ready` once,
each job until it has reported a success once. It costs nothing on Cloudflare's free plan.

The jobs report through `deploy/report.sh`: the three crontab lines of step 2.7 run each job
through it. It runs the job exactly as before, its output still going to the job's log, and then
tells the watcher `ok` or `fail` with one short line (for example the backup's
`BACKUP FAILED while ...` words; never anything of a reader). If the watcher cannot be reached,
the job is not affected and its log ends with `report.sh: could not tell the watcher ...`. Until
the two `MOTHLI_WATCH_` settings are in `deploy/.env`, it reports nothing.

**Once: setting it up**, after the first backup has worked (step 4.1; before that every night's
backup fails, and would email you). Your domain's Email Routing must be on, with your inbox
listed and **Verified** under Email Routing > Destination addresses: the watcher can send only to
such an address. Steps 1 to 4 are done on the computer that puts the watcher in place (it needs
Node.js and a bash terminal: on Windows, Git Bash), in the Mothli folder.

1. Sign Wrangler, Cloudflare's tool, in to your account. A browser page opens; check the account
   and choose Allow:
   ```
   npx wrangler@4 login --scopes account:read user:read workers:write workers_scripts:write workers_kv:write
   ```
   This lets Wrangler on this computer see your account and change your Workers and their storage,
   nothing else (without `--scopes` it would ask for everything Wrangler can do, billing included).
2. Put the watcher in place:
   ```
   cd deploy/watch
   npx wrangler@4 deploy
   ```
   The last lines show its address, `https://mothli-watch.SOMETHING.workers.dev`: keep it for
   step 5. If Wrangler asks for a workers.dev subdomain, any short name will do. If it wrote an
   `"id"` into `wrangler.jsonc`, leave that change out of git (`git checkout wrangler.jsonc`).
3. Make the watcher's token and send it to both places without it ever being shown: to the
   watcher, and over SSH into the server's `deploy/.env` (SERVER is how you reach the server with
   `ssh`; `~/mothli` is where Mothli is checked out there):
   ```
   token=$(openssl rand -hex 32)
   printf '%s' "$token" | npx wrangler@4 secret put WATCH_TOKEN
   printf '%s\n' "$token" | ssh SERVER '~/mothli/deploy/setup.sh set MOTHLI_WATCH_TOKEN'
   unset token
   ```
   The server answers `MOTHLI_WATCH_TOKEN ... looks right`. Nobody needs to keep a copy: to make a
   new one, run the same four lines with `set MOTHLI_WATCH_TOKEN --replace`.
4. Give the watcher its other settings, one at a time, still in `deploy/watch`; each asks for the
   value:
   ```
   npx wrangler@4 secret put SITE_URL
   npx wrangler@4 secret put MAIL_FROM
   npx wrangler@4 secret put MAIL_TO
   ```
   `SITE_URL`: `https://YOURDOMAIN`. `MAIL_FROM`: any address at your domain, for example
   `alerts@YOURDOMAIN` (it needs no mailbox). `MAIL_TO`: your verified inbox. While Paddle's API
   key is not in yet (step 4c), also `npx wrangler@4 secret put JOBS_OFF` with the value
   `paddle-check` (later: Cloudflare > Workers & Pages > mothli-watch > Settings > Variables and
   Secrets, and delete `JOBS_OFF`). Then `npx wrangler@4 logout`.
5. On the server, give it the watcher's address of step 2 (it asks for it):
   ```
   deploy/setup.sh set MOTHLI_WATCH_URL
   ```
   `deploy/setup.sh check` no longer lists the two `MOTHLI_WATCH_` settings under "Needed later".
6. Try it, on the server:
   ```
   deploy/report.sh backup -- false
   deploy/report.sh backup -- true
   ```
   Within a minute: "Mothli: last night's backup failed", then "Mothli: the nightly backup works
   again". This counts as the backup's first success, so from now on a night without a backup
   report emails you. Nothing came? Check the spam folder, then run `deploy/setup.sh check`;
   `report.sh` says `could not tell the watcher` when it cannot reach it.

**Turning one watch off** (for example the Paddle check before Paddle is set up): the watcher's
setting `JOBS_OFF`, the jobs not to watch separated by commas, as in step 4. To stop everything,
Cloudflare > Workers & Pages > mothli-watch > Settings > Trigger events: delete the cron trigger.

## 6b. Measuring what paying readers use and what the calls cost

Before and after a change meant to make the chat cheaper, take the numbers (read only; it writes
nothing):
```
docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.measure
```
`--days 7` looks at the last 7 UTC days instead of 30. It prints one line of JSON, numbers and
account ids only (never a title, a word of a conversation or an email address):

- `readers`: one entry per paying reader (`state` `paying`, as `app.hosted.plans show` says):
  `replies_per_day` for every day of the window, `replies_per_month` for every month they have
  talked with Mothli, and `unique_uploads`, how many books on their shelf are files they uploaded
  that no other account has (Mothli builds that book's story memory for them alone). A reply is
  one writer call of a reply in a book or on the home page; its checks and second looks are not
  counted.
- `calls`: every account's calls in the window, per `purpose` and `phase`: how many, and the
  totals of full-price input, input written to the provider's cache, input read from it, output,
  and `usd`. `calls_without_split` counts calls whose cache split is not known (from before this
  was recorded, a failed call, a check or an embedding); their whole input counts as full price.

To compare, run it for the same number of days before the change and again after it.

## 7. When something is wrong

- **A reader says the site is down.** Run the two `curl` lines of step 6.
  - `live` fails too: `docker compose -f deploy/compose.yaml ps`, then
    `docker compose -f deploy/compose.yaml up -d --wait`.
  - `ready` says `not_ready`: run the deployment step by hand; it names what is wrong:
    ```
    docker compose -f deploy/compose.yaml run --rm migrate
    ```
    `refused: ... is not set` means a setting is missing in `deploy/.env`. `failed: ...` names
    the check that failed. Then `docker compose -f deploy/compose.yaml restart web`.
- **The web process stops right after starting.** `docker compose -f deploy/compose.yaml logs
  --tail 50 web`. A line `hosted mode requires NAME` names a missing setting in
  `deploy/app.env`; `the streamed chat in hosted mode requires NAME` means one AI key is filled in
  and the other is not (fill in both, or empty both).
- **A scheduled job's log (`deploy/backups/purge.log`, for example) or the worker's ends in
  `ValidationError ... requires ...`.** The job read `deploy/app.env` and stopped before doing
  anything, so nothing was changed. The line names what is missing. If it names a `PADDLE_` setting or
  `HOSTED_BILLING_DSN`, the server's code is older than the one that keeps Paddle's settings to
  the web process: `git pull` and `docker compose -f deploy/compose.yaml up -d --build --wait`
  (step 3), then run the job by hand once (`docker compose -f deploy/compose.yaml run --rm
  purge`). Otherwise fill in what it names, as for the web process above.
- **Readers see no chat.** Both AI keys in `deploy/app.env` are empty (or the file was made from
  an older template whose chat lines were commented out: copy the chat section from
  `deploy/app.env.example`). Fill them in, then
  `docker compose -f deploy/compose.yaml up -d --wait`; readers see the chat after their next
  sign-in or page reload.
- **A reader is stuck at a limit.** See their limits (leave out `--owner READER_ID` to list
  everyone; the ids are also in the audit log):
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.limits show --owner READER_ID
  ```
  Only a paying reader can reach one (ADR 0046: talking with Mothli is the plan from the first
  question); setting a reader's own day or month limit with `app.hosted.limits` replaces the
  default caps for them. A reader who does not pay is refused by the plan, whatever their limits:
  give them a gift (below) to let them in.
- **Mothli on the house: your own account and test accounts** (ADR 0045, gifts). A gifted account
  is a paying reader without Paddle, held only by a paying reader's hidden day and month guards ($1.00 and $4.00), or by its own limits if you set
  them with `app.hosted.limits`. Find the account's id in the audit log (below; sign in with it
  first), then give it a gift for good:
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.plans gift ACCOUNT_ID
  ```
  or until the end of a day (UTC), for a tester:
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.plans gift ACCOUNT_ID --until 2027-06-30
  ```
  Look at where an account stands (the state and the gift's date; never a book or a word of a
  conversation):
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.plans show ACCOUNT_ID
  ```
  It prints one line, for example
  `{"gift_until": "for good", "owner_id": "…", "paid_by": "gift", "state": "paying", "why": null}`.
  `state` is `new` (never had a plan or a gift), `ended` (with `why` `plan`: a plan or a gift
  that stopped) or `paying` (`paid_by`: `gift` or `paddle`, step 4c). End a gift with
  `... python -m app.hosted.plans gift ACCOUNT_ID --end`; the account is then wherever it would
  be without it: `ended` if it has had a Paddle plan, else `new`. Either way Mothli is closed to
  it until it pays. (A gift that simply runs out keeps its date, so the account shows `ended`.)
  `HOSTED_FREE_DAYS` and `HOSTED_FREE_USD` are gone (ADR 0046); if `deploy/app.env` still has
  them, they are ignored, and the lines can be deleted.
- **"Add to my books" in the free library always says Mothli couldn't get the book.** The web
  process fetches each book itself, over HTTPS, from Project Gutenberg's `gutenberg.pglaf.org`
  and `www.gutenberg.org` (nowhere else, and through no proxy). If your server's provider or a
  firewall limits outbound traffic, allow those two hosts on port 443. To check from the server:
  `docker compose -f deploy/compose.yaml exec web python -c "import httpx; print(httpx.head('https://gutenberg.pglaf.org/').status_code)"`.
- **What happened to an account.** The audit log holds no content, only what happened and when:
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.audit show --owner READER_ID --limit 50
  ```
- **The server is out of disk.** `df -h`. Backup files left in `deploy/backups/` by a failed upload
  (step 4.2) can go once a later backup has worked: `rm deploy/backups/mothli-backup-*.tar.age`.
- Anything else: save the output of `docker compose -f deploy/compose.yaml logs --tail 300 > problem.txt`
  and hand it to whoever looks after the code. It holds no reader content and no password.

## 8. Only you can fill in

`deploy/setup.sh check` lists, by name, whichever of these the settings files still lack.

- The domain name (`MOTHLI_DOMAIN`), and adding it to Cloudflare.
- The Cloudflare tunnel and its token (`CLOUDFLARE_TUNNEL_TOKEN`), the rate-limit rule and the
  cache rule.
- The server and its size.
- The sign-in service: which one, and its issuer, client ID and client secret (`OIDC_*`).
- The two AI accounts and their keys, for Mothli's chat: the writer (`OPENAI_API_KEY`, or
  `ANTHROPIC_API_KEY`) and the checker, TypeSafe (`TYPESAFE_API_KEY`), in `deploy/app.env`.
- The spending caps: $1.00 per reader per day and $4.00 per reader per month (a paying or gifted
  reader's hidden guards, decided 23 November 2026, ADR 0045), $15 a day for the whole platform
  and $10 a day for story memory (ADR 0044) are the owner's numbers. There are no free weeks
  (ADR 0046): talking with Mothli is the plan from the first question.
- Paddle (step 4c): Mothli's product and its two price ids, the notification destination and its
  secret key, the API key and its expiry date, in the sandbox first and then live (`PADDLE_*` in
  `deploy/app.env`), the regular check in the crontab, and the billing role's password
  (`MOTHLI_DB_BILLING_PASSWORD` in `deploy/.env`).
- Who runs Mothli, for the public pages (step 4d): `OPERATOR_LEGAL_NAME`, `OPERATOR_COUNTRY` and
  `OPERATOR_CONTACT_EMAIL` in `deploy/app.env`, and a lawyer's reading of the terms and the
  privacy notice before going live.
- How long backups are kept, and where the off-server copies go. That is also how long a deleted
  account's data lives on in backups (step 4), and the privacy page says two weeks: change both
  together.
- Where the offline copy of `deploy/.env` and `deploy/app.env` is kept.
