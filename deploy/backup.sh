#!/usr/bin/env bash
# Back up Mothli to Cloudflare R2: the database and the book files, as one file a day, keeping the
# newest 14 there (runbook step 4). Started every night by the server's crontab:
#   0 3 * * * cd ~/mothli && deploy/backup.sh >> deploy/backups/backup.log 2>&1
# or by hand the same way. The app keeps running. Every line it prints starts with the UTC time; the
# last one says "backup ok" or "BACKUP FAILED", and a failure exits non-zero.
#
# The file (mothli-backup-<UTC time>.tar.age) is a tar encrypted on this server, before it is
# uploaded, with the backup key MOTHLI_BACKUP_ENCRYPTION_KEY from deploy/.env (an age file; the key
# is NOT in the backup and never in R2). Inside the tar:
#   database.dump   every table, in pg_restore's format: accounts, reading, notes, conversations,
#                   Mothli's story memory and chapter notes, spend records, the audit log
#   books.tar       the readers' book files exactly as on disk (also encrypted with
#                   HOSTED_STORAGE_FILESYSTEM_KEY, which is NOT in the backup)
#   counts.txt      rows per table, read back from database.dump (the restore compares them)
#   SHA256SUMS      checksums of the three, checked before a restore trusts them
#
# The dump is taken inside the database container by the superuser over the container's own socket:
# the superuser is the one role row-level security never hides a row from, and so no database
# password leaves that container. Nothing in R2 is removed unless the new file is there whole.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${MOTHLI_COMPOSE:-docker compose -f deploy/compose.yaml}
DIR=deploy/backups
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

step="starting"
nothing_removed="nothing was uploaded and no backup in R2 was removed"
trap 'log "BACKUP FAILED while $step: $nothing_removed" >&2; exit 1' ERR

umask 077
mkdir -p "$DIR"
exec 9>"$DIR/.backup.lock"
if ! flock -n 9; then
  log "BACKUP FAILED: another backup is still running" >&2
  exit 1
fi

started=$(date +%s)
name="mothli-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.age"
step="checking the backup key"
$COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups check-key

work="$DIR/${name%.tar.age}"
mkdir "$work"
log "backup started: $name"

step="dumping the database"
$COMPOSE exec -T db sh -c 'exec pg_dump -U postgres -d "$POSTGRES_DB" --format=custom' \
  > "$work/database.dump"

step="reading the dump back (row counts)"
$COMPOSE exec -T db pg_restore --data-only --file=- < "$work/database.dump" \
  | $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups count-rows \
  > "$work/counts.txt"
grep -q '^users [0-9]' "$work/counts.txt"
log "database: $(wc -l < "$work/counts.txt") tables, $(du -h "$work/database.dump" | cut -f1)"

step="copying the book files"
# Book files are written once and never changed, so tar's "file changed or removed while reading"
# (exit 1) can only mean a reader removed a book that moment; anything worse fails the backup.
$COMPOSE run --rm --no-deps -T --entrypoint sh backup -c \
  'tar -C /var/lib/mothli/books --exclude=".*.tmp" -cf - . ; rc=$?; [ "$rc" -le 1 ] || exit "$rc"' \
  > "$work/books.tar"
log "book files: $(tar -tf "$work/books.tar" | grep -c '\.epub\.enc$' || true), $(du -h "$work/books.tar" | cut -f1)"

step="writing checksums and the encrypted backup file"
(cd "$work" && sha256sum database.dump counts.txt books.tar > SHA256SUMS)
# The tar goes straight into the encryption: no unencrypted copy of the whole backup is written.
tar -C "$work" --remove-files -cf - database.dump counts.txt books.tar SHA256SUMS \
  | $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups encrypt > "$DIR/$name"
rmdir "$work"
# The size of the encrypted file, which is what is uploaded and checked in the bucket.
size=$(stat -c %s "$DIR/$name")

step="uploading to R2"
nothing_removed="the backup file is kept on the server as $DIR/$name (see the line above)"
$COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups upload "$name" --size "$size" \
  < "$DIR/$name"

# In R2 now: drop this file and anything an earlier failed run left here.
rm -f "$DIR"/mothli-backup-*.tar.age
rm -rf "$DIR"/mothli-backup-*/
log "backup ok: $name, $(( size / 1000000 )) MB, in $(( $(date +%s) - started )) s"
