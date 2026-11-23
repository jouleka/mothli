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
   - SSL/TLS > Overview: **Full (strict)**.
   - SSL/TLS > Edge Certificates: **Always Use HTTPS** on.
   - Caching > Cache Rules: a rule "URI Path starts with `/api/`" > **Bypass cache**.
   - Security > WAF > Rate limiting rules: a rule "URI Path starts with `/api/auth/`", for
     example 20 requests per minute per IP, action **Block**.
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

1. Get the code:
   ```
   git clone https://github.com/jouleka/mothli.git ~/mothli
   cd ~/mothli
   ```
2. Create the two settings files from the templates and make them private:
   ```
   cp deploy/.env.example deploy/.env
   cp deploy/app.env.example deploy/app.env
   chmod 600 deploy/.env deploy/app.env
   ```
3. Make one password for each `..._PASSWORD` line in `deploy/.env` (run this once per line and
   paste each result after the `=`):
   ```
   openssl rand -hex 32
   ```
4. Make the key in `deploy/app.env` for `HOSTED_STORAGE_FILESYSTEM_KEY`, which encrypts every
   book file:
   ```
   openssl rand -base64 32
   ```
5. Fill in the rest: in `deploy/.env` your domain and the tunnel token; in `deploy/app.env` the
   sign-in issuer, client ID and client secret. Edit with:
   ```
   nano deploy/.env
   nano deploy/app.env
   ```
   **For Mothli's chat**, also fill in the two AI keys in `deploy/app.env`: `OPENAI_API_KEY` (or
   `ANTHROPIC_API_KEY` instead) and `TYPESAFE_API_KEY`. Everything else the chat needs is already
   set there; leave it as it is. Then turn on the job worker, which writes Mothli's notes on
   finished chapters, what it remembers about readers and its notes on every book's story (step
   4b): in `deploy/.env`, remove the `#` in front of this line:
   ```
   COMPOSE_PROFILES=worker
   ```
   Leave both keys empty and the server starts without a chat (and without the worker line).
   Fill in only one and the web process stops at startup with a line naming the missing one
   (step 7 below).
6. **Copy both files somewhere safe and offline now** (a password manager or an encrypted USB
   stick). Without `HOSTED_STORAGE_FILESYSTEM_KEY` no book can ever be read again, backups
   included.
7. Build and start:
   ```
   docker compose -f deploy/compose.yaml up -d --build --wait
   ```
   The first build takes several minutes. `--wait` returns when everything is healthy.
8. Check it (step 5 below), then open `https://YOURDOMAIN` and sign in.

## 3. Updating

```
cd ~/mothli
git pull
docker compose -f deploy/compose.yaml up -d --build --wait
```

The `migrate` step runs first on every start: it updates the database and resets every
database permission to exactly what the code expects. If it fails, the new web process does not
start; see "When something is wrong".

## 4. Backups (every night, to Cloudflare R2)

Every night at 03:00 the server makes one backup file and puts it in a private Cloudflare R2
bucket, `mothli-protected-backups`. The newest 14 are kept there; older ones are removed, but only
after the new one has arrived whole. The app keeps running while it happens.

**What is in a backup:** the whole database (accounts, everything readers keep in their books,
their conversations with Mothli, Mothli's chapter notes and story memory, spend records, the
audit log) and the readers' book files. Story memory is in it on purpose: making it again would
cost about $2.60 for every 30 chapters.

**What is not, on purpose:** the two settings files `deploy/.env` and `deploy/app.env` (they hold
the keys; keep your offline copy from step 2.6), the database passwords and roles (made again from
`deploy/.env` when you restore), the app itself (it is in git) and the logs.

**The book files are still encrypted** inside the backup, with `HOSTED_STORAGE_FILESYSTEM_KEY`
from `deploy/app.env`. Without your offline copy of that file no backed-up book can be read again.
**The database part is not encrypted by Mothli**: it holds readers' emails and conversations, and
it is protected by R2's own encryption, the bucket being private, and the key below working for
this one bucket only. Anyone who has that key, or gets into your Cloudflare account, can read it:
turn on two-step sign-in for your Cloudflare account.

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
3. Cloudflare now shows the token's values **once**. Keep that page open, and on the server open
   the settings file:
   ```
   nano deploy/.env
   ```
   Paste, after the `=` of each line (no spaces, no quotes):
   - **Access Key ID** into `MOTHLI_BACKUP_ACCESS_KEY_ID=`
   - **Secret Access Key** into `MOTHLI_BACKUP_SECRET_ACCESS_KEY=`
   - the address under **Use jurisdiction-specific endpoints for S3 clients** (it looks like
     `https://` + a long code + `.r2.cloudflarestorage.com`) into `MOTHLI_BACKUP_S3_ENDPOINT=`

   The first value shown, "Token value", is not needed. If your `deploy/.env` was made before
   these lines existed, copy the "Backups to Cloudflare R2" block from `deploy/.env.example` into
   it first. Save with Ctrl+O, Enter, Ctrl+X.
4. Check that the server can reach the bucket:
   ```
   docker compose -f deploy/compose.yaml run --rm backup
   ```
   It ends with `0 backups in mothli-protected-backups`. A line starting `backup failed:` names
   what is wrong (`... is not set in deploy/.env` means a value is missing).
5. Make the first backup by hand:
   ```
   cd ~/mothli
   deploy/backup.sh
   ```
   The last line says `backup ok: mothli-backup-<date and time>.tar, ... MB, in ... s`.
6. Make it run every night at 03:00 (before the 04:00 erasing of step 4a): run `crontab -e` and
   add this line:
   ```
   0 3 * * * cd ~/mothli && deploy/backup.sh >> deploy/backups/backup.log 2>&1
   ```

### 4.2 Did last night's backup run?

```
tail -n 4 deploy/backups/backup.log
docker compose -f deploy/compose.yaml run --rm backup
```

The log's last line should be `backup ok: ...` with last night's date; the second command lists
what is in the bucket, newest first, with sizes. Look once a week; nothing warns you on its own.

If the log's last line starts `BACKUP FAILED while ...`, the backup did not happen and **nothing
in R2 was removed**. The words after `while` say which step failed, and the line above it often
says why. Run `deploy/backup.sh` by hand to see it again. Usual causes: the R2 lines in
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

Run it every day at 04:00 with `crontab -e`:

```
0 4 * * * cd ~/mothli && docker compose -f deploy/compose.yaml run --rm purge >> deploy/backups/purge.log 2>&1
```

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
`erased 1, unfinished 0, waiting 3; editions erased 0, unfinished 0, waiting 2`: accounts erased
in this run, accounts it could not finish (it ends with an error then), and accounts still inside
their 30 days; then the same for book files' notes. `tail deploy/backups/purge.log` shows the last runs. An
account it could not finish (book storage unreachable, the database restarted) is finished by the
next run; nothing of it is shown to anyone meanwhile. The audit log of an erased account ends with
`account.purge`:

```
docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.audit show --owner READER_ID
```

## 4b. Story memory: who's who and the story so far

Mothli keeps notes on every reader's book, uploaded or from the free library: who's who and the
story so far. They are made once for each exact book file, a chapter at a time, as readers reach
the chapters (one chapter ahead of the reader furthest on), and each reader is handed them only
up to where they are (ADR 0044). There is nothing to run and no title to choose: it is on in
`deploy/app.env` (`HOSTED_STORY_MEMORY=true`, as the template sets it) and is done by the job
worker, so the worker must be on (`COMPOSE_PROFILES=worker` in `deploy/.env`, step 2.5). With no
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

## 5. Restoring

### 5.1 Test a backup (once a month, on the live server)

```
cd ~/mothli
deploy/restore.sh --test latest
```

It downloads the newest backup, checks its checksums, restores it into a scratch database
(`mothli_restore_test`) beside the live one, checks that every table has the backup's row count
and which migration the database is at, counts the book files, then deletes the scratch database.
Readers notice nothing, and the live database is not touched. It needs free disk of about twice
the backup's size (`df -h`). The last line says `TEST RESTORE OK: ...`. Anything else ends with
`TEST RESTORE FAILED while ...`: keep that output and look at the backups before you need one.

To test an older one, name it instead of `latest` (the names come from
`docker compose -f deploy/compose.yaml run --rm backup`).

### 5.2 Restore for real

Only into an empty Mothli: a new server, or this one after removing everything. Restoring an
older backup loses everything readers did after it was made. You need the **same** `deploy/app.env`
the backup was made with (the book files' key) and a `deploy/.env` with the R2 lines filled in.

1. On a new server: steps 1.2, 2.1 and 2.2, then put your offline copies of `deploy/.env` and
   `deploy/app.env` over the new ones (`chmod 600 deploy/.env deploy/app.env` again).
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
   1. downloads the backup and checks its checksums;
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
6. Make sure the nightly backup is in your crontab on this server (step 4.1, point 6).

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
  A reader in their two free weeks (ADR 0045: the 14 days after their first question,
  `HOSTED_FREE_DAYS`) is not held by the day and month caps but by one ceiling for the whole free
  weeks, `HOSTED_FREE_USD` ($2.10) of their own chat; past it they are told their free start is
  used up, and on day 15 the day and month caps apply again. Setting a reader's own day or month
  limit with `app.hosted.limits` takes them out of that rule: their own limits then apply, free
  weeks or not. Clearing both puts them back under it.
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
  (step 4.2) can go once a later backup has worked: `rm deploy/backups/mothli-backup-*.tar`.
- Anything else: save the output of `docker compose -f deploy/compose.yaml logs --tail 300 > problem.txt`
  and hand it to whoever looks after the code. It holds no reader content and no password.

## 8. Only you can fill in

- The domain name (`MOTHLI_DOMAIN`), and adding it to Cloudflare.
- The Cloudflare tunnel and its token (`CLOUDFLARE_TUNNEL_TOKEN`), the rate-limit rule and the
  cache rule.
- The server and its size.
- The sign-in service: which one, and its issuer, client ID and client secret (`OIDC_*`).
- The two AI accounts and their keys, for Mothli's chat: the writer (`OPENAI_API_KEY`, or
  `ANTHROPIC_API_KEY`) and the checker, TypeSafe (`TYPESAFE_API_KEY`), in `deploy/app.env`.
- The spending caps: $0.10 per reader per day and $2.00 per reader per month are placeholders to
  confirm or change; $15 a day for the whole platform and $10 a day for story memory are the
  owner's numbers (ADR 0044). A reader's two free weeks and their $2.10 ceiling
  (`HOSTED_FREE_DAYS`, `HOSTED_FREE_USD`) are ADR 0045's proposal, also to confirm or change.
- How long backups are kept, and where the off-server copies go. That is also how long a deleted
  account's data lives on in backups (step 4).
- Where the offline copy of `deploy/.env` and `deploy/app.env` is kept.
