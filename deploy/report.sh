#!/usr/bin/env bash
# Run one of the server's scheduled jobs and tell Mothli's watcher how it went (runbook, "Alerts:
# how Mothli tells you something is wrong"):
#   deploy/report.sh backup -- deploy/backup.sh
#   deploy/report.sh purge -- docker compose -f deploy/compose.yaml run --rm purge
#   deploy/report.sh paddle-check -- docker compose -f deploy/compose.yaml exec -T web python -m app.hosted.paddle_check
# The job's output passes through unchanged and its exit status is this script's exit status. Then
# one report goes to the watcher: ok, or fail with one short line of reason that holds no reader
# data. The report waits at most 10 seconds and never fails the job: if the watcher cannot be
# reached, one line says so and that is all.
#
# Reads MOTHLI_WATCH_URL and MOTHLI_WATCH_TOKEN from deploy/.env (only those two lines; the file
# is never loaded whole). Either one empty or missing: the job runs as before, no report is sent.
# The token is never printed, nor put on a command line where other users of the server could see.
set -uo pipefail

if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
  echo "usage: deploy/report.sh backup|purge|paddle-check -- COMMAND [ARGS...]" >&2
  exit 2
fi
job=$1
shift 2
case "$job" in
  backup | purge | paddle-check) ;;
  *) echo "report.sh: unknown job '$job' (backup, purge or paddle-check)" >&2; exit 2 ;;
esac

env_file="$(cd "$(dirname "$0")" && pwd)/.env"
setting() {
  # The last NAME=value line, without surrounding quotes or spaces. Nothing else is read.
  [ -r "$env_file" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$env_file" | tail -n 1 |
    sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Run the job: stdout and stderr each pass through to where they were going, and a copy of both is
# kept here for the reason line.
work=$(mktemp -d "${TMPDIR:-/tmp}/mothli-report.XXXXXX") || exit 1
trap 'rm -rf "$work"' EXIT
{ { "$@"; echo "$?" > "$work/status"; } 2>&1 1>&3 3>&- | tee -a "$work/output" >&2; } 3>&1 |
  tee -a "$work/output"
status=$(cat "$work/status" 2>/dev/null || echo 127)

url=$(setting MOTHLI_WATCH_URL)
token=$(setting MOTHLI_WATCH_TOKEN)
[ -n "$url" ] && [ -n "$token" ] || exit "$status"
case "$token" in
  *[!A-Za-z0-9_-]*)
    echo "report.sh: MOTHLI_WATCH_TOKEN in deploy/.env must be letters and digits only; no report sent" >&2
    exit "$status" ;;
esac

if [ "$status" -eq 0 ]; then
  outcome=ok
  reason=""
else
  outcome=fail
  reason="it stopped with exit status $status"
  case "$job" in
    backup)
      # backup.sh's own words: the step that failed and whether anything was uploaded or removed.
      line=$(grep -o 'BACKUP FAILED.*' "$work/output" | tail -n 1)
      [ -n "$line" ] && reason=$line ;;
    purge)
      # purge.py's count line: "erased 0, unfinished 0, held 1, waiting 3; editions ..."
      if grep -Eq '(^|[^a-z])held [1-9]' "$work/output"; then
        reason="an account is waiting on Paddle to cancel its plan, so it was not erased"
      elif grep -Eq 'unfinished [1-9]' "$work/output"; then
        reason="some accounts or book files could not be finished; the next run tries again"
      fi ;;
    paddle-check)
      line=$(grep -Eo 'checked [0-9]+, changed [0-9]+, failed [0-9]+' "$work/output" | tail -n 1)
      [ -n "$line" ] && reason=$line ;;
  esac
fi
reason=$(printf '%s' "$reason" | tr -c '[:print:]' ' ' | cut -c 1-300)

# The address and the token go to curl on its standard input, not on its command line.
if ! printf 'url = "%s/report/%s/%s"\nheader = "Authorization: Bearer %s"\n' \
    "${url%/}" "$job" "$outcome" "$token" |
  curl --config - --silent --fail --max-time 10 --output /dev/null \
    --header "Content-Type: text/plain" --data-raw "$reason" 2>/dev/null; then
  echo "report.sh: could not tell the watcher that $job was $outcome (the job itself is not affected)" >&2
fi
exit "$status"
