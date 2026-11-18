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
   finished chapters and what it remembers about readers: in `deploy/.env`, remove the `#` in
   front of this line:
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

## 4. Backups

Make a backup (the app keeps running):

```
cd ~/mothli
deploy/backup.sh
```

It writes a folder `deploy/backups/<date and time>/` with the database, the book files (still
encrypted) and checksums. **Copy that folder off the server** every time, for example to your
computer:

```
scp -r YOURUSER@YOURSERVER:mothli/deploy/backups/FOLDERNAME .
```

To make one every night at 03:00, run `crontab -e` and add this line:

```
0 3 * * * cd ~/mothli && deploy/backup.sh >> deploy/backups/backup.log 2>&1
```

How many to keep, and where the copies live, is your decision (see the list at the end).
Delete old ones with `rm -r deploy/backups/FOLDERNAME`.

**Backups still hold deleted accounts.** When a reader's account is erased (next section), it is
gone from the server, but every backup made before that day still holds it, books included,
until that backup is deleted. So a reader's data really disappears only once the last backup
holding it is gone. The privacy text has to say this, with how long backups are kept.

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

**Checking it ran.** Each run prints one line, for example `erased 1, unfinished 0, waiting 3`:
accounts erased in this run, accounts it could not finish (it ends with an error then), and
accounts still inside their 30 days. `tail deploy/backups/purge.log` shows the last runs. An
account it could not finish (book storage unreachable, the database restarted) is finished by the
next run; nothing of it is shown to anyone meanwhile. The audit log of an erased account ends with
`account.purge`:

```
docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.audit show --owner READER_ID
```

## 4b. Preparing the free library's story memory (when you choose)

For a book from the free library, Mothli can keep who's who and the story so far, prepared once
for everyone and handed to each reader only up to where they are. You choose which titles; nothing
is prepared until you run this. It needs the worker on (`COMPOSE_PROFILES=worker` in
`deploy/.env`) and `HOSTED_STORY_MEMORY=true` in `deploy/app.env`. A reader's own uploads are not
touched by any of this.

1. Start it for the titles you want, by their free-library numbers (the number in the book's
   Project Gutenberg address, for example 1342 for *Pride and Prejudice*):
   ```
   docker compose -f deploy/compose.yaml run --rm library python -m app.hosted.library_memory build 1342 11
   ```
   The first run makes the library account, a reserved account nobody signs in to, and prints
   its id (`library account ...`). Each title is fetched and added to that account as a reader's
   add would, and the worker then prepares it one chapter at a time. A title that could not be
   added prints the reader's own line for it (a full shelf, for example: raise the account's
   `--max-books` or `--max-library-bytes` with the limits tool below, then run `build` again).
2. **Set what it may spend** (about $7 for a novel of that length at today's prices). It spends
   only from the library account's own allowance, never a reader's, and the platform's daily cap
   still applies to it. For example $10 a day:
   ```
   docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.limits set LIBRARY_ACCOUNT_ID --max-spend-usd-per-day 10 --max-spend-usd-per-month 50
   ```
   Until then it uses the readers' default caps, which prepare about one chapter a day.
3. See how each title stands, as often as you like:
   ```
   docker compose -f deploy/compose.yaml run --rm library
   ```
   One line per title, for example
   `1342 Pride and Prejudice: built through chapter 12 of 61, 0 unremembered, $1.0412, building`.
   "unremembered" counts chapters Mothli could not make notes on (readers see a short line for
   each). "waiting for tomorrow" means a day's cap was reached. "stopped: three chapters in a row
   could not be remembered" means the model is failing and nothing more is spent on that title;
   look at the AI account before trying again.

Running `build` again is safe: a title already prepared is left alone and one half prepared
carries on. A reader whose copy of a title is a different file (Project Gutenberg changed it)
gets no prepared notes for it. The library command has its own database role
(`MOTHLI_DB_LIBRARY_PASSWORD` in `deploy/.env`), which only this step has.

## 5. Restoring

Only into an empty Mothli (a new server, or after removing everything). You need the backup
folder and the **same** `deploy/.env` and `deploy/app.env` it was made with.

1. Put the code, the two settings files and the backup folder on the server (steps 2.1 and 2.2,
   then copy your saved files over the new ones).
2. Only if this server already ran Mothli and you mean to replace everything on it:
   ```
   docker compose -f deploy/compose.yaml down --volumes
   ```
   This deletes the database and all book files on this server.
3. Build, then restore:
   ```
   docker compose -f deploy/compose.yaml build
   deploy/restore.sh deploy/backups/FOLDERNAME
   ```
   It refuses if the database or the book folder is not empty. At the end it compares every
   table's row count with the backup's and says so.

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
- **"Add to my books" in the free library always says Mothli couldn't get the book.** The web
  process fetches each book itself, over HTTPS, from Project Gutenberg's `gutenberg.pglaf.org`
  and `www.gutenberg.org` (nowhere else, and through no proxy). If your server's provider or a
  firewall limits outbound traffic, allow those two hosts on port 443. To check from the server:
  `docker compose -f deploy/compose.yaml exec web python -c "import httpx; print(httpx.head('https://gutenberg.pglaf.org/').status_code)"`.
- **What happened to an account.** The audit log holds no content, only what happened and when:
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.audit show --owner READER_ID --limit 50
  ```
- **The server is out of disk.** `df -h`, then delete old backup folders (after copying them off).
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
- The spending caps: $0.10 per reader per day, $2.00 per reader per month and $5.00 per day for
  the whole platform are placeholders to confirm or change.
- How long backups are kept, and where the off-server copies go. That is also how long a deleted
  account's data lives on in backups (step 4).
- Where the offline copy of `deploy/.env` and `deploy/app.env` is kept.
