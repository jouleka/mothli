#!/bin/sh
# Restore a backup made by deploy/backup.sh into a FRESH stack (empty database, no book files):
#   deploy/restore.sh deploy/backups/<UTC time>
# deploy/.env and deploy/app.env must be the ones the backup was made with (same
# HOSTED_STORAGE_FILESYSTEM_KEY, or no book can be read). It refuses to touch a database or a
# book volume that already holds anything.
set -eu
cd "$(dirname "$0")/.."
COMPOSE=${MOTHLI_COMPOSE:-docker compose -f deploy/compose.yaml}
DB=${MOTHLI_DB_NAME:-mothli}
src=${1:?usage: deploy/restore.sh <backup folder>}
started=$(date +%s)

echo "1/7 checking the backup's checksums"
(cd "$src" && sha256sum -c --quiet SHA256SUMS)

echo "2/7 starting the database"
$COMPOSE up -d --wait db

echo "3/7 checking that the database and the book volume are empty"
tables=$($COMPOSE exec -T db psql -U postgres -d "$DB" -qtAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
files=$($COMPOSE run --rm --no-deps -T --entrypoint sh web -c 'ls -A /var/lib/mothli/books | wc -l')
if [ "$tables" != "0" ] || [ "$files" != "0" ]; then
  echo "refused: this stack already holds data ($tables tables, $files book entries)" >&2
  exit 1
fi

echo "4/7 creating the database roles"
$COMPOSE run --rm --no-deps -T migrate python scripts/hosted_roles.py prepare

echo "5/7 restoring the database"
$COMPOSE exec -T db pg_restore -U postgres -d "$DB" --exit-on-error --single-transaction \
  < "$src/database.dump"

echo "6/7 restoring the book files"
gunzip -c "$src/books.tar.gz" \
  | $COMPOSE run --rm --no-deps -T --entrypoint tar web -C /var/lib/mothli/books -xf -

echo "7/7 starting everything (grants, schema marker and role checks run first)"
$COMPOSE up -d --wait

$COMPOSE exec -T db psql -U postgres -d "$DB" -qtA -v ON_ERROR_STOP=1 < deploy/counts.sql \
  > "${TMPDIR:-/tmp}/mothli-restored-counts.txt"
if ! diff "$src/counts.txt" "${TMPDIR:-/tmp}/mothli-restored-counts.txt" >/dev/null; then
  echo "WARNING: row counts differ from the backup's:" >&2
  diff "$src/counts.txt" "${TMPDIR:-/tmp}/mothli-restored-counts.txt" >&2 || true
  exit 1
fi
echo "restored from $src in $(( $(date +%s) - started )) s; every table has the backup's row count"
