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
   - Note its **issuer** address, **client ID** and **client secret**.
7. **The AI accounts** (OpenAI or Anthropic, and TypeSafe for Jev), only to switch on Mothli's
   chat inside a book. Mothli runs without them; the AI section of `deploy/app.env` says which
   lines to fill in to switch the chat on.

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
4. Make the two keys in `deploy/app.env` (once for `HOSTED_CREDENTIAL_MASTER_KEY`, once for
   `HOSTED_STORAGE_FILESYSTEM_KEY`):
   ```
   openssl rand -base64 32
   ```
5. Fill in the rest: in `deploy/.env` your domain and the tunnel token; in `deploy/app.env` the
   sign-in issuer, client ID and client secret. Edit with:
   ```
   nano deploy/.env
   nano deploy/app.env
   ```
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
  `deploy/app.env`.
- **A reader is stuck at a limit.** See their limits (leave out `--owner READER_ID` to list
  everyone; the ids are also in the audit log):
  ```
  docker compose -f deploy/compose.yaml run --rm tools python -m app.hosted.limits show --owner READER_ID
  ```
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
- The AI accounts and keys, once the companion is switched on (names in `deploy/app.env`).
- The spending caps: $0.10 per reader per day, $2.00 per reader per month and $5.00 per day for
  the whole platform are placeholders to confirm or change.
- How long backups are kept, and where the off-server copies go.
- Where the offline copy of `deploy/.env` and `deploy/app.env` is kept.
