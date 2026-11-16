#!/bin/sh
# Back up Mothli: the database and the book files, into one new folder.
#   deploy/backup.sh                 # into deploy/backups/<UTC time>/
#   deploy/backup.sh /some/folder    # into /some/folder/<UTC time>/
# Run from anywhere on the server, as a user allowed to run docker. The stack keeps running.
#
# The folder holds no key: the book files are encrypted with HOSTED_STORAGE_FILESYSTEM_KEY and
# the database dump holds no password. Keep deploy/.env and deploy/app.env (the keys) apart from
# the backups, offline. Copy each backup folder off the server (see the runbook).
set -eu
cd "$(dirname "$0")/.."
COMPOSE=${MOTHLI_COMPOSE:-docker compose -f deploy/compose.yaml}
DB=${MOTHLI_DB_NAME:-mothli}
started=$(date +%s)
dest="${1:-deploy/backups}/$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$dest"

# 1. The database first: every book a row in this dump names is already on disk, so step 2 copies it.
$COMPOSE exec -T db pg_dump -U postgres -d "$DB" --format=custom > "$dest/database.dump"
$COMPOSE exec -T db psql -U postgres -d "$DB" -qtA -v ON_ERROR_STOP=1 < deploy/counts.sql > "$dest/counts.txt"

# 2. The book files, as they are on disk (already encrypted).
$COMPOSE run --rm --no-deps -T --entrypoint tar web -C /var/lib/mothli/books -cf - . \
  | gzip > "$dest/books.tar.gz"

# 3. Checksums, so a copy can be checked before it is trusted.
(cd "$dest" && sha256sum database.dump counts.txt books.tar.gz > SHA256SUMS)

echo "backup written to $dest in $(( $(date +%s) - started )) s"
du -sh "$dest" | cut -f1 | sed 's/^/size: /'
