#!/usr/bin/env bash
# Restore a backup made by deploy/backup.sh (runbook step 5).
#
#   deploy/restore.sh --test latest     # test a backup: restore it into a scratch database beside
#                                       # the live one, check it, delete the scratch copy. Changes
#                                       # nothing readers see; safe on the live server.
#   deploy/restore.sh latest            # restore into an EMPTY Mothli (new server, or after
#                                       # `docker compose -f deploy/compose.yaml down --volumes`)
#
# Instead of `latest`: a backup's name from `docker compose -f deploy/compose.yaml run --rm backup`
# (mothli-backup-...tar.age), an encrypted backup file already on this server, or a folder holding
# one already decrypted and unpacked by hand (runbook step 5.3). A plain, unencrypted tar file is
# refused: no such backup is ever made. deploy/.env and deploy/app.env must be the ones the backup
# was made with (the same MOTHLI_BACKUP_ENCRYPTION_KEY, or the backup cannot be opened, and the same
# HOSTED_STORAGE_FILESYSTEM_KEY, or no book can be read). A full restore refuses to touch a database
# or a book volume that already holds anything. Every line it prints starts with the UTC time; the
# last one says "RESTORE OK" or "RESTORE FAILED", and a failure exits non-zero.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${MOTHLI_COMPOSE:-docker compose -f deploy/compose.yaml}
DIR=deploy/backups
SCRATCH=mothli_restore_test
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

test_only=no
if [ "${1:-}" = "--test" ]; then test_only=yes; shift; fi
which=${1:?usage: deploy/restore.sh [--test] latest|BACKUP_NAME|FILE|FOLDER}
step="starting"
trap 'log "RESTORE FAILED while $step" >&2; exit 1' ERR
started=$(date +%s)
umask 077
mkdir -p "$DIR"

# ---- 1. The backup, decrypted and unpacked into deploy/backups/restore/, checksums checked -----
if [ -d "$which" ]; then
  src=$which
  log "1/6 using the folder $which as it is (already decrypted and unpacked)"
else
  step="checking the backup key"
  $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups check-key
  step="getting the backup"
  src=$DIR/restore
  rm -rf "$src"
  mkdir -p "$src"
  if [ -f "$which" ]; then
    file=$which
  else
    file=$DIR/restore-download.tar.age
    log "1/6 downloading $which from R2"
    $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups download "$which" > "$file"
  fi
  # Only after every chunk passed its check does this step succeed; a damaged file stops here, and
  # whatever was unpacked before the damage is deleted.
  step="decrypting the backup"
  $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups decrypt < "$file" \
    | tar -C "$src" -xf - || { rm -rf "$src"; false; }
fi
tidy() {  # the unpacked copy holds readers' data: keep it no longer than needed
  rm -f "$DIR/restore-counts.diff"
  if [ "$src" = "$DIR/restore" ]; then rm -rf "$DIR/restore" "$DIR/restore-download.tar.age"; fi
}
step="checking the backup's checksums"
(cd "$src" && sha256sum -c --quiet SHA256SUMS)
log "1/6 backup unpacked in $src, checksums good"

# The newest migration in the backup and in this checkout.
code_version=$(ls backend/app/hosted/schema/ | grep -E '^[0-9]{4}_.*\.sql$' | sort | tail -n 1 | cut -c1-4)
counts() {  # rows per table in database $1, in counts.txt's order
  $COMPOSE exec -T db psql -U postgres -d "$1" -qtA -v ON_ERROR_STOP=1 < deploy/counts.sql
}
check() {  # database $1 against the backup: rows in every table, and the migration it is at
  step="comparing row counts"
  if ! diff <(counts "$1") "$src/counts.txt" > "$DIR/restore-counts.diff"; then
    log "row counts differ from the backup's (left: restored, right: backup):" >&2
    cat "$DIR/restore-counts.diff" >&2
    false
  fi
  log "every table has the backup's row count ($(wc -l < "$src/counts.txt") tables, $(awk '{s+=$2} END {print s}' "$src/counts.txt") rows)"
  step="reading the migration version"
  restored_version=$($COMPOSE exec -T db psql -U postgres -d "$1" -qtA -v ON_ERROR_STOP=1 \
    -c 'SELECT lpad(max(version)::text, 4, $$0$$) FROM app_schema_migrations')
  log "the backup's database is at migration $restored_version; this code's newest is $code_version"
  if [ "$restored_version" \> "$code_version" ]; then
    log "this backup was made by newer code than this checkout: run git pull, then again" >&2
    false
  fi
}

step="starting the database"
$COMPOSE up -d --wait db

if [ "$test_only" = yes ]; then
  # ---- Test: into a scratch database beside the live one, then delete it -------------------
  drop_scratch() {
    $COMPOSE exec -T db psql -U postgres -d postgres -qX -v ON_ERROR_STOP=1 \
      -c "SET client_min_messages = warning" \
      -c "DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)" > /dev/null
  }
  trap 'log "TEST RESTORE FAILED while $step (the live database was not touched)" >&2; drop_scratch || true; exit 1' ERR
  step="making the scratch database $SCRATCH"
  drop_scratch
  $COMPOSE exec -T db psql -U postgres -d postgres -qX -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE $SCRATCH" -c "REVOKE ALL ON DATABASE $SCRATCH FROM PUBLIC" > /dev/null
  log "2/6 scratch database $SCRATCH made (readers' database untouched)"
  step="restoring into the scratch database"
  $COMPOSE exec -T db pg_restore -U postgres -d "$SCRATCH" --exit-on-error --single-transaction \
    < "$src/database.dump"
  log "3/6 database restored into $SCRATCH"
  check "$SCRATCH"
  log "4/6 checks passed"
  step="reading the book files"
  log "5/6 book files in the backup: $(tar -tf "$src/books.tar" | grep -c '\.epub\.enc$' || true)"
  step="deleting the scratch database"
  drop_scratch
  log "6/6 scratch database deleted"
  tidy
  log "TEST RESTORE OK: $(basename "$which") restores whole, in $(( $(date +%s) - started )) s"
  exit 0
fi

# ---- Full restore, into an empty Mothli -------------------------------------------------------
step="checking that the database and the book volume are empty"
tables=$($COMPOSE exec -T db sh -c 'exec psql -U postgres -d "$POSTGRES_DB" -qtAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = '"'public'"'"')
files=$($COMPOSE run --rm --no-deps -T --entrypoint sh web -c 'ls -A /var/lib/mothli/books | wc -l')
if [ "$tables" != "0" ] || [ "$files" != "0" ]; then
  log "RESTORE REFUSED: this Mothli already holds data ($tables tables, $files book entries); see the runbook" >&2
  tidy
  exit 1
fi
log "2/6 the database and the book volume are empty"

step="creating the database roles"
$COMPOSE run --rm --no-deps -T migrate python scripts/hosted_roles.py prepare

step="restoring the database"
$COMPOSE exec -T db sh -c \
  'exec pg_restore -U postgres -d "$POSTGRES_DB" --exit-on-error --single-transaction' \
  < "$src/database.dump"
log "3/6 database restored"

step="restoring the book files"
$COMPOSE run --rm --no-deps -T --entrypoint tar web -C /var/lib/mothli/books -xf - \
  < "$src/books.tar"
log "4/6 book files restored: $(tar -tf "$src/books.tar" | grep -c '\.epub\.enc$' || true)"

check "$($COMPOSE exec -T db sh -c 'printf %s "$POSTGRES_DB"')"
log "5/6 checks passed"

step="starting Mothli (roles, migrations, grants and the app's own checks run first)"
$COMPOSE up -d --wait
log "6/6 every service is up and healthy (web answers ready)"
tidy
log "RESTORE OK: $(basename "$which") in $(( $(date +%s) - started )) s. Now open the site and sign in."
