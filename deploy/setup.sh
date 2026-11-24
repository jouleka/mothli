#!/usr/bin/env bash
# Prepare Mothli's two settings files on the server (runbook step 2). Run it from the checkout:
#
#   deploy/setup.sh [--domain NAME --oidc-issuer URL ...]   make deploy/.env and deploy/app.env, fill
#                                         in every password and the book files' key, and write the
#                                         values given (or asked for) that are not secret
#   deploy/setup.sh set NAME [--replace]  paste one value (input hidden) into the right file
#   deploy/setup.sh check                 name what is still to fill in, and who fills it in
#   deploy/setup.sh backup-key            make the backup key with Mothli's own image (once, ever)
#   deploy/setup.sh cron [--show]         put the three scheduled lines in this user's crontab
#   deploy/setup.sh worker on|off         turn the job worker on or off (COMPOSE_PROFILES=worker)
#
# It never prints a value, a part of one or its length, and never replaces a value that is
# already there unless `set` is told to. Running it twice changes nothing. It touches only the two
# settings files, this user's crontab (its own three marked lines) and, for backup-key, Mothli's
# own compose project: no other container, network, project or tunnel on this machine.
set -Eeuo pipefail
umask 077
export LC_ALL=C

cd "$(dirname "$0")/.."
ROOT=$(pwd -P)
ENV_FILE=deploy/.env
APP_FILE=deploy/app.env
ENV_EXAMPLE=deploy/.env.example
APP_EXAMPLE=deploy/app.env.example
COMPOSE=${MOTHLI_COMPOSE:-docker compose -f deploy/compose.yaml}

PASSWORDS="POSTGRES_PASSWORD MOTHLI_DB_OWNER_PASSWORD MOTHLI_DB_AUTH_PASSWORD
MOTHLI_DB_TENANT_PASSWORD MOTHLI_DB_WORKER_PASSWORD MOTHLI_DB_AUDIT_PASSWORD
MOTHLI_DB_OPERATOR_PASSWORD MOTHLI_DB_PURGE_PASSWORD MOTHLI_DB_BILLING_PASSWORD"

say() { printf '%s\n' "$*"; }
fail() { printf 'setup: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  deploy/setup.sh [options]          make the settings files, the passwords and the book files' key
  deploy/setup.sh set NAME [--replace]
  deploy/setup.sh check
  deploy/setup.sh backup-key
  deploy/setup.sh cron [--show]
  deploy/setup.sh worker on|off

Options for the first form (each is asked for when left out and this is a terminal; Enter skips):
  --domain NAME                 MOTHLI_DOMAIN, without https://
  --oidc-issuer URL             OIDC_ISSUER, exactly as the sign-in service shows it
  --oidc-client-id ID           OIDC_CLIENT_ID
  --operator-name TEXT          OPERATOR_LEGAL_NAME
  --operator-country TEXT       OPERATOR_COUNTRY
  --operator-email ADDRESS      OPERATOR_CONTACT_EMAIL
  --paddle-environment ENV      PADDLE_ENVIRONMENT: sandbox or production
  --paddle-price-monthly IDS    PADDLE_PRICE_MONTHLY (pri_...)
  --paddle-price-yearly IDS     PADDLE_PRICE_YEARLY (pri_...)
  --paddle-checkout-url URL     PADDLE_CHECKOUT_URL (default: https://DOMAIN/)
  --r2-endpoint URL             MOTHLI_BACKUP_S3_ENDPOINT
  --r2-bucket NAME              MOTHLI_BACKUP_BUCKET (default: mothli-protected-backups)
  --no-prompt                   ask for nothing
EOF
}

# ---- Reading and writing one line --------------------------------------------------------------

# Which file a setting belongs in, from the two example files: prints the file, or nothing.
file_of() {
  if grep -Eq "^(#[[:space:]]*)?$1=" "$ENV_EXAMPLE"; then say "$ENV_FILE"
  elif grep -Eq "^(#[[:space:]]*)?$1=" "$APP_EXAMPLE"; then say "$APP_FILE"
  fi
}

known_names() {
  cat "$ENV_EXAMPLE" "$APP_EXAMPLE" | sed -nE 's/^(#[[:space:]]*)?([A-Z][A-Z0-9_]*)=.*/\2/p' \
    | grep -v '^COMPOSE_PROFILES$' | sort -u
}

# set, empty, placeholder, commented or absent (the active line wins over a commented one).
state_of() {
  [ -f "$1" ] || { say absent; return; }
  awk -v n="$2" '
    BEGIN { st = "absent" }
    $0 ~ "^" n "=" {
      v = substr($0, length(n) + 2); sub(/\r$/, "", v); gsub(/^[ \t]+|[ \t]+$/, "", v)
      if (v == "") st = "empty"
      else if (toupper(v) ~ /OWNER_MUST_FILL_IN|GENERATE_WITH_/) st = "placeholder"
      else st = "set"
      found = 1; exit
    }
    $0 ~ "^#[ \t]*" n "=" { if (st == "absent") st = "commented" }
    END { print st }' "$1"
}

# The active line's value, for the checks below; never printed by them.
value_of() {
  [ -f "$1" ] || return 0
  awk -v n="$2" '$0 ~ "^" n "=" { v = substr($0, length(n) + 2); sub(/\r$/, "", v);
                                  gsub(/^[ \t]+|[ \t]+$/, "", v); print v; exit }' "$1"
}

# Write NAME=VALUE in place of its active line, else its commented line, else at the end. The
# value travels in the environment, never on a command line another user could see.
write_line() {
  local file=$1 name=$2 mode tmp
  case $(state_of "$file" "$name") in
    commented) mode=commented ;;
    absent) mode=append ;;
    *) mode=active ;;
  esac
  tmp=$(mktemp "$file.XXXXXX")
  SETUP_VALUE=$3 awk -v n="$name" -v mode="$mode" '
    BEGIN { v = ENVIRON["SETUP_VALUE"] }
    !done && ((mode == "active" && $0 ~ "^" n "=") ||
              (mode == "commented" && $0 ~ "^#[ \t]*" n "=")) { print n "=" v; done = 1; next }
    { print }
    END { if (!done) print n "=" v }' "$file" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

# Keep a value on a commented line (it stays off until its partner is set).
write_commented() {
  local file=$1 name=$2 tmp
  tmp=$(mktemp "$file.XXXXXX")
  SETUP_VALUE=$3 awk -v n="$name" '
    BEGIN { v = ENVIRON["SETUP_VALUE"] }
    !done && $0 ~ "^#[ \t]*" n "=" { print "# " n "=" v; done = 1; next }
    { print }' "$file" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

# Turn a commented line on as it is.
uncomment_line() {
  local file=$1 name=$2 tmp
  tmp=$(mktemp "$file.XXXXXX")
  awk -v n="$name" '
    !done && $0 ~ "^#[ \t]*" n "=" { sub(/^#[ \t]*/, ""); done = 1 }
    { print }' "$file" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

commented_value_state() {
  # Whether the commented line of NAME holds something other than a placeholder: real or none.
  awk -v n="$2" '
    $0 ~ "^#[ \t]*" n "=" { v = $0; sub(/^#[ \t]*[A-Z_0-9]*=/, "", v)
      if (v != "" && toupper(v) !~ /OWNER_MUST_FILL_IN|GENERATE_WITH_/) r = "real"; exit }
    END { print (r == "" ? "none" : r) }' "$1"
}

# ---- What each value must look like (the deployment's and the app's own rules) -----------------

paddle_env() {
  local e
  e=$(value_of "$APP_FILE" PADDLE_ENVIRONMENT)
  [ "$e" = production ] && say production || say sandbox
}

# The settings the owner pastes as secrets: each must be one word, and `set` never writes one that
# breaks its rule (pasting the command itself into the hidden prompt is easy to do, twice).
is_pasted_secret() {
  case $1 in
    *_PASSWORD) return 1 ;;
    OIDC_CLIENT_SECRET|CLOUDFLARE_TUNNEL_TOKEN|MOTHLI_WATCH_TOKEN) return 0 ;;
    *_SECRET|*_TOKEN|*_API_KEY|*_ACCESS_KEY|*_ACCESS_KEY_ID|*_SECRET_ACCESS_KEY|*_CLIENT_TOKEN) return 0 ;;
  esac
  return 1
}

TUNNEL_TOKEN_RE='^eyJ[A-Za-z0-9_=-]{97,}$'

# What is wrong with VALUE for NAME, as one plain phrase after "was not written:" or "does not
# look right:"; nothing when it is fine. Never the value.
problem_of() {
  local name=$1 v=$2 rule
  if is_pasted_secret "$name" && [[ $v == *setup.sh* ]]; then
    say "that looks like the command, not the value: copy the value first, then run the command and paste"
    return
  fi
  rule=$(expected "$name" "$v")
  [ -z "$rule" ] || say "expected $rule"
}

# Prints what was expected when VALUE does not fit NAME's rule; prints nothing when it does.
expected() {
  local rule
  rule=$(expected_rule "$@")
  if [ -z "$rule" ] && is_pasted_secret "$1" && [[ $2 == *[[:space:]]* ]]; then
    rule="one word, with no spaces inside (just the value, nothing around it)"
  fi
  [ -z "$rule" ] || say "$rule"
}

expected_rule() {
  local name=$1 v=$2 env
  case $name in
    CLOUDFLARE_TUNNEL_TOKEN)
      [[ $v =~ $TUNNEL_TOKEN_RE ]] \
        || say "the tunnel's token as Cloudflare shows it: one word starting eyJ, 100 or more letters, digits, - _ or =" ;;
    *_PASSWORD)
      [[ $v =~ ^[A-Za-z0-9_-]{32,128}$ ]] || say "32 to 128 letters, digits, - or _" ;;
    HOSTED_STORAGE_FILESYSTEM_KEY)
      [[ $v =~ ^[A-Za-z0-9+/]{43}=$ ]] || say "32 random bytes in base64 (deploy/setup.sh makes it)" ;;
    MOTHLI_BACKUP_ENCRYPTION_KEY)
      [[ $v =~ ^AGE-SECRET-KEY-1[0-9A-Z]{50,}$ ]] \
        || say "a line starting AGE-SECRET-KEY-1 (deploy/setup.sh backup-key makes it)" ;;
    MOTHLI_DOMAIN)
      [[ $v =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
        || say "a domain name in small letters, without https:// or a slash" ;;
    OIDC_ISSUER)
      { [[ $v =~ ^https://[^[:space:]?#@]+$ ]] && [[ $v != *// ]]; } \
        || say "an https:// address with no query, as the sign-in service shows it" ;;
    OIDC_CLIENT_ID)
      [[ $v =~ ^[A-Za-z0-9._~:@/+=-]{1,200}$ ]] || say "the client ID, on one line, no spaces" ;;
    OPERATOR_LEGAL_NAME|OPERATOR_COUNTRY)
      { [ ${#v} -ge 2 ] && [ ${#v} -le 200 ] && [[ $v != *[\<\>\{\}\\\$\"]* ]] \
        && [[ $v != *" #"* ]] && [[ $v != \'* ]]; } \
        || say "plain text of 2 to 200 characters, without < > { } \\ \$ \" or ' at the start" ;;
    OPERATOR_CONTACT_EMAIL)
      [[ $v =~ ^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}$ ]] \
        || say "one email address" ;;
    PADDLE_ENVIRONMENT)
      [[ $v =~ ^(sandbox|production)$ ]] || say "sandbox or production" ;;
    PADDLE_PRICE_MONTHLY|PADDLE_PRICE_YEARLY)
      [[ $v =~ ^pri_[a-z0-9]{26}(,pri_[a-z0-9]{26})*$ ]] \
        || say "Paddle price ids (pri_...), separated by commas" ;;
    PADDLE_CHECKOUT_URL)
      [[ $v =~ ^https://[a-z0-9.-]+/$ ]] || say "https://YOUR-DOMAIN/ with nothing after the slash" ;;
    PADDLE_WEBHOOK_SECRET)
      [[ $v =~ ^pdl_ntfset_[^[:space:]]{16,}$ ]] \
        || say "the notification destination's secret key, as Paddle shows it (pdl_ntfset_...)" ;;
    PADDLE_API_KEY)
      env=$(paddle_env)
      if [ "$env" = production ]; then
        [[ $v =~ ^pdl_live_apikey_[^[:space:]]{16,}$ ]] || say "a live API key (pdl_live_apikey_...), because PADDLE_ENVIRONMENT is production"
      else
        [[ $v =~ ^pdl_sdbx_apikey_[^[:space:]]{16,}$ ]] || say "a sandbox API key (pdl_sdbx_apikey_...), because PADDLE_ENVIRONMENT is sandbox"
      fi ;;
    PADDLE_CLIENT_TOKEN)
      env=$(paddle_env)
      if [ "$env" = production ]; then
        [[ $v =~ ^live_[^[:space:]]{16,}$ ]] || say "a live client-side token (live_...), because PADDLE_ENVIRONMENT is production"
      else
        [[ $v =~ ^test_[^[:space:]]{16,}$ ]] || say "a sandbox client-side token (test_...), because PADDLE_ENVIRONMENT is sandbox"
      fi ;;
    MOTHLI_BACKUP_S3_ENDPOINT)
      [[ $v =~ ^https://[A-Za-z0-9.-]+/?$ ]] || say "the https:// address Cloudflare shows, with no bucket name after it" ;;
    MOTHLI_WATCH_URL)
      [[ $v =~ ^https://[A-Za-z0-9.-]+/?$ ]] || say "the watcher's https:// address, with nothing after the name (step 6a)" ;;
    MOTHLI_WATCH_TOKEN)
      [[ $v =~ ^[A-Za-z0-9_-]{32,}$ ]] || say "32 or more letters, digits, - or _ (step 6a makes it)" ;;
    MOTHLI_BACKUP_BUCKET)
      [[ $v =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || say "a bucket name in small letters, digits and -" ;;
  esac
  # Every value: one line, nothing that Docker Compose would read as a comment or a variable.
  case $v in
    *'$'*) say "a value without the \$ sign (Docker Compose would read it as a variable)" ;;
    *' #'*) say "a value without \" #\" (Docker Compose would cut it there)" ;;
  esac
}

# ---- The first form: make the files and fill them in --------------------------------------------

random_hex() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  else head -c 32 /dev/urandom | od -An -vtx1 | tr -d ' \n'; fi
}
random_base64() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 32
  else head -c 32 /dev/urandom | base64; fi
}

made=() kept=() written=()

make_file() {
  local file=$1 example=$2
  [ -f "$example" ] || fail "$example is missing: run this from a full checkout"
  if [ ! -f "$file" ]; then
    cp "$example" "$file"
    say "made $file from $example"
  fi
  chmod 600 "$file"
}

fill_secret() {
  local file=$1 name=$2 kind=$3 value
  case $(state_of "$file" "$name") in
    set) kept+=("$name"); return ;;
  esac
  if [ "$kind" = hex ]; then value=$(random_hex); else value=$(random_base64); fi
  write_line "$file" "$name" "$value"
  value=
  made+=("$name")
}

# Write a value that is not secret, given as an option or typed at the prompt.
fill_plain() {
  local name=$1 value=$2 question=$3 file problem
  file=$(file_of "$name")
  if [ "$(state_of "$file" "$name")" = set ]; then
    [ -n "$value" ] && kept+=("$name")
    return 0
  fi
  if [ -z "$value" ] && [ "$PROMPT" = yes ]; then
    read -r -p "$question (Enter to skip): " value || value=
  fi
  value=$(printf '%s' "$value" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  [ -n "$value" ] || return 0
  problem=$(expected "$name" "$value")
  [ -z "$problem" ] || fail "$name was not written: expected $problem"
  write_line "$file" "$name" "$value"
  written+=("$name")
}

cmd_setup() {
  local domain="" issuer="" client="" oname="" ocountry="" oemail="" penv="" pmonth="" pyear=""
  local pcheckout="" r2="" bucket="" name
  PROMPT=no
  [ -t 0 ] && PROMPT=yes
  while [ $# -gt 0 ]; do
    local opt=$1 val=""
    case $opt in
      --no-prompt) PROMPT=no; shift; continue ;;
      -h|--help) usage; exit 0 ;;
      --*=*) val=${opt#*=}; opt=${opt%%=*}; shift ;;
      --*) [ $# -ge 2 ] || fail "$opt needs a value"; val=$2; shift 2 ;;
      *) usage >&2; exit 2 ;;
    esac
    case $opt in
      --domain) domain=$val ;;
      --oidc-issuer) issuer=$val ;;
      --oidc-client-id) client=$val ;;
      --operator-name) oname=$val ;;
      --operator-country) ocountry=$val ;;
      --operator-email) oemail=$val ;;
      --paddle-environment) penv=$val ;;
      --paddle-price-monthly) pmonth=$val ;;
      --paddle-price-yearly) pyear=$val ;;
      --paddle-checkout-url) pcheckout=$val ;;
      --r2-endpoint) r2=$val ;;
      --r2-bucket) bucket=$val ;;
      *) fail "unknown option $opt (deploy/setup.sh --help lists them)" ;;
    esac
  done

  make_file "$ENV_FILE" "$ENV_EXAMPLE"
  make_file "$APP_FILE" "$APP_EXAMPLE"

  for name in $PASSWORDS; do fill_secret "$ENV_FILE" "$name" hex; done
  fill_secret "$APP_FILE" HOSTED_STORAGE_FILESYSTEM_KEY base64

  fill_plain MOTHLI_DOMAIN "$domain" "Your domain, without https:// (MOTHLI_DOMAIN)"
  fill_plain OIDC_ISSUER "$issuer" "The sign-in service's issuer address (OIDC_ISSUER)"
  fill_plain OIDC_CLIENT_ID "$client" "The sign-in service's client ID (OIDC_CLIENT_ID)"
  fill_plain OPERATOR_LEGAL_NAME "$oname" "Your legal name or your company's (OPERATOR_LEGAL_NAME)"
  fill_plain OPERATOR_COUNTRY "$ocountry" "The country you run Mothli from (OPERATOR_COUNTRY)"
  fill_plain OPERATOR_CONTACT_EMAIL "$oemail" "The address readers write to (OPERATOR_CONTACT_EMAIL)"
  fill_plain PADDLE_ENVIRONMENT "$penv" "Paddle: sandbox or production (PADDLE_ENVIRONMENT)"
  if [ "$(state_of "$APP_FILE" PADDLE_ENVIRONMENT)" = set ]; then
    fill_plain PADDLE_PRICE_MONTHLY "$pmonth" "Paddle's monthly price id (PADDLE_PRICE_MONTHLY)"
    fill_plain PADDLE_PRICE_YEARLY "$pyear" "Paddle's yearly price id (PADDLE_PRICE_YEARLY)"
    # The three Paddle values you paste get lines of their own, empty until then (empty is off).
    for name in PADDLE_WEBHOOK_SECRET PADDLE_API_KEY PADDLE_CLIENT_TOKEN; do
      if [ "$(state_of "$APP_FILE" "$name")" = commented ]; then write_line "$APP_FILE" "$name" ""; fi
    done
    checkout_url "$pcheckout"
  fi
  fill_plain MOTHLI_BACKUP_S3_ENDPOINT "$r2" "Cloudflare R2's S3 address for the backups (MOTHLI_BACKUP_S3_ENDPOINT)"
  if [ -n "$bucket" ]; then fill_plain MOTHLI_BACKUP_BUCKET "$bucket" ""; fi

  [ ${#made[@]} -eq 0 ] || say "made: ${made[*]}"
  [ ${#written[@]} -eq 0 ] || say "written: ${written[*]}"
  [ ${#kept[@]} -eq 0 ] || say "already set, kept as they are: ${kept[*]}"
  if [ ${#made[@]} -eq 0 ] && [ ${#written[@]} -eq 0 ]; then say "nothing to change"; fi
  say "Next: deploy/setup.sh check"
}

# PADDLE_CHECKOUT_URL is refused by the app without PADDLE_API_KEY, so it waits on its commented
# line until the key is pasted, and this turns it on then (setup, or set PADDLE_API_KEY).
checkout_url() {
  local given=$1 domain problem
  case $(state_of "$APP_FILE" PADDLE_CHECKOUT_URL) in
    set) ;;
    commented)
      if [ -z "$given" ] && [ "$(commented_value_state "$APP_FILE" PADDLE_CHECKOUT_URL)" = none ]; then
        domain=$(value_of "$ENV_FILE" MOTHLI_DOMAIN)
        [ "$(state_of "$ENV_FILE" MOTHLI_DOMAIN)" = set ] && given="https://$domain/"
      fi
      if [ -n "$given" ]; then
        problem=$(expected PADDLE_CHECKOUT_URL "$given")
        [ -z "$problem" ] || fail "PADDLE_CHECKOUT_URL was not written: expected $problem"
        write_commented "$APP_FILE" PADDLE_CHECKOUT_URL "$given"
        written+=("PADDLE_CHECKOUT_URL (kept off until PADDLE_API_KEY is in)")
      fi ;;
  esac
  if [ "$(state_of "$APP_FILE" PADDLE_API_KEY)" = set ] \
      && [ "$(state_of "$APP_FILE" PADDLE_CHECKOUT_URL)" = commented ] \
      && [ "$(commented_value_state "$APP_FILE" PADDLE_CHECKOUT_URL)" = real ]; then
    uncomment_line "$APP_FILE" PADDLE_CHECKOUT_URL
    say "PADDLE_CHECKOUT_URL is on now that PADDLE_API_KEY is in"
  fi
}

# ---- set NAME: paste one value ------------------------------------------------------------------

cmd_set() {
  local name="" replace=no file state value answer problem
  while [ $# -gt 0 ]; do
    case $1 in
      --replace) replace=yes ;;
      -*) fail "unknown option $1" ;;
      *) [ -z "$name" ] || fail "one NAME at a time"; name=$1 ;;
    esac
    shift
  done
  [ -n "$name" ] || fail "which setting? deploy/setup.sh set NAME"
  file=""
  [[ $name =~ ^[A-Z][A-Z0-9_]*$ ]] && file=$(file_of "$name")
  if [ -z "$file" ] || [ "$name" = COMPOSE_PROFILES ]; then
    printf 'setup: %s is not a setting this deployment knows. Known names:\n' "$name" >&2
    known_names | sed 's/^/  /' >&2
    exit 1
  fi
  [ -f "$file" ] || fail "$file does not exist yet: run deploy/setup.sh first"
  state=$(state_of "$file" "$name")
  if [ "$state" = set ] && [ "$replace" = no ]; then
    if [ -t 0 ]; then
      read -r -p "$name already has a value. Replace it? [y/N] " answer || answer=
      case $answer in y|Y|yes|Yes|YES) ;; *) say "kept $name as it was"; exit 0 ;; esac
    else
      say "$name already has a value: kept as it was (add --replace to change it)"
      exit 0
    fi
  fi
  if [ -t 0 ]; then
    read -rs -p "Paste $name and press Enter: " value || value=
    printf '\n'
  else
    value=$(cat) || value=
  fi
  value=${value%$'\r'}
  value=$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; printf x)
  value=${value%x}
  case $value in
    *$'\n'*|*$'\r'*) fail "$name was not written: the value has more than one line" ;;
  esac
  [ -n "$value" ] || fail "$name was not written: nothing was pasted"
  # Cloudflare's page shows the token inside an install command: copying that whole command is
  # natural, so exactly one token-shaped word in it is kept.
  if [ "$name" = CLOUDFLARE_TUNNEL_TOKEN ] && [[ $value == *[[:space:]]* ]] \
      && [[ $value != *setup.sh* ]]; then
    local word found="" count=0
    set -f  # split into words only: never expand a * in the pasted text into file names
    for word in $value; do
      if [[ $word =~ $TUNNEL_TOKEN_RE ]]; then found=$word; count=$((count + 1)); fi
    done
    set +f
    word=
    if [ "$count" -eq 1 ]; then
      value=$found
      say "kept the token from the command you pasted"
    fi
    found=
  fi
  problem=$(problem_of "$name" "$value")
  if [ -n "$problem" ] && is_pasted_secret "$name"; then
    value=
    fail "$name was not written: $problem"
  fi
  write_line "$file" "$name" "$value"
  value=
  say "$name written to $file"
  if [ -n "$problem" ]; then
    say "$name does not look right: $problem"
  else
    say "$name looks right"
  fi
  if [ "$name" = PADDLE_API_KEY ] && [ "$(state_of "$APP_FILE" PADDLE_CHECKOUT_URL)" = commented ]; then
    checkout_url ""
  fi
  [ -z "$problem" ]
}

# ---- check: what is still to fill in ------------------------------------------------------------

blocking=() later=()

who() {
  case $1 in
    *_PASSWORD|HOSTED_STORAGE_FILESYSTEM_KEY) say "deploy/setup.sh makes it" ;;
    MOTHLI_BACKUP_ENCRYPTION_KEY) say "deploy/setup.sh backup-key makes it, once the images are built" ;;
    MOTHLI_DOMAIN) say "deploy/setup.sh --domain" ;;
    OIDC_ISSUER) say "deploy/setup.sh --oidc-issuer" ;;
    OIDC_CLIENT_ID) say "deploy/setup.sh --oidc-client-id" ;;
    OPERATOR_LEGAL_NAME) say "deploy/setup.sh --operator-name" ;;
    OPERATOR_COUNTRY) say "deploy/setup.sh --operator-country" ;;
    OPERATOR_CONTACT_EMAIL) say "deploy/setup.sh --operator-email" ;;
    PADDLE_ENVIRONMENT) say "deploy/setup.sh --paddle-environment" ;;
    PADDLE_PRICE_MONTHLY) say "deploy/setup.sh --paddle-price-monthly" ;;
    PADDLE_PRICE_YEARLY) say "deploy/setup.sh --paddle-price-yearly" ;;
    PADDLE_CHECKOUT_URL) say "deploy/setup.sh turns it on once PADDLE_API_KEY is in" ;;
    MOTHLI_BACKUP_S3_ENDPOINT) say "deploy/setup.sh --r2-endpoint" ;;
    CLOUDFLARE_TUNNEL_TOKEN) say "you paste it: deploy/setup.sh set CLOUDFLARE_TUNNEL_TOKEN (Cloudflare, your tunnel)" ;;
    OIDC_CLIENT_SECRET) say "you paste it: deploy/setup.sh set OIDC_CLIENT_SECRET (your sign-in service)" ;;
    MOTHLI_BACKUP_ACCESS_KEY_ID|MOTHLI_BACKUP_SECRET_ACCESS_KEY) say "you paste it: deploy/setup.sh set $1 (Cloudflare R2 token)" ;;
    PADDLE_*) say "you paste it: deploy/setup.sh set $1 (Paddle)" ;;
    MOTHLI_WATCH_URL) say "you paste it: deploy/setup.sh set MOTHLI_WATCH_URL (the watcher's address, step 6a)" ;;
    MOTHLI_WATCH_TOKEN) say "sent from the computer that puts the watcher in place, unseen (step 6a)" ;;
    TYPESAFE_API_KEY) say "you paste it: deploy/setup.sh set TYPESAFE_API_KEY (TypeSafe, for Mothli's chat)" ;;
    OPENAI_API_KEY) say "you paste it, last of all: deploy/setup.sh set OPENAI_API_KEY (or ANTHROPIC_API_KEY)" ;;
    *) say "you fill it in: deploy/setup.sh set $1" ;;
  esac
}

# Look at one setting: missing, a placeholder, or a value that breaks its rule.
look() {
  local file=$1 name=$2 group=$3 state value problem
  state=$(state_of "$file" "$name")
  case $state in
    set)
      value=$(value_of "$file" "$name")
      problem=$(problem_of "$name" "$value")
      value=
      if [ -n "$problem" ]; then blocking+=("$name|$file|does not look right: $problem"); fi ;;
    *)
      if [ "$group" = start ]; then blocking+=("$name|$file|$(who "$name")")
      else later+=("$name|$file|$(who "$name")"); fi ;;
  esac
}

is_set() { [ "$(state_of "$1" "$2")" = set ]; }

# The chat's two keys: the writer's (OpenAI or Anthropic) and the checker's. Prints none, one or both.
chat_keys() {
  local writer=no checker=no
  if is_set "$APP_FILE" OPENAI_API_KEY || is_set "$APP_FILE" ANTHROPIC_API_KEY; then writer=yes; fi
  is_set "$APP_FILE" TYPESAFE_API_KEY && checker=yes
  case $writer$checker in
    yesyes) say both ;;
    nono) say none ;;
    yesno) say writer ;;
    *) say checker ;;
  esac
}

cmd_check() {
  local name keys
  local chat=()
  for f in "$ENV_FILE" "$APP_FILE"; do
    [ -f "$f" ] || { say "$f does not exist yet: run deploy/setup.sh first"; exit 1; }
  done

  # Needed for Mothli to start.
  look "$ENV_FILE" MOTHLI_DOMAIN start
  look "$ENV_FILE" CLOUDFLARE_TUNNEL_TOKEN start
  for name in $PASSWORDS; do look "$ENV_FILE" "$name" start; done
  look "$APP_FILE" HOSTED_STORAGE_FILESYSTEM_KEY start
  look "$APP_FILE" OIDC_ISSUER start
  look "$APP_FILE" OIDC_CLIENT_ID start
  look "$APP_FILE" OIDC_CLIENT_SECRET start

  # The chat: both keys, or neither (then Mothli starts without a chat). A key that is in must be
  # one word, like every pasted secret.
  for name in TYPESAFE_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
    if is_set "$APP_FILE" "$name"; then look "$APP_FILE" "$name" start; fi
  done
  keys=$(chat_keys)
  if [ "$keys" = writer ]; then
    blocking+=("TYPESAFE_API_KEY|$APP_FILE|$(who TYPESAFE_API_KEY); the writer key is in, so the chat needs this one too")
  elif [ "$keys" = checker ]; then
    blocking+=("OPENAI_API_KEY|$APP_FILE|$(who OPENAI_API_KEY); TYPESAFE_API_KEY is in, so the chat needs this one too")
  elif [ "$keys" = none ]; then
    chat=("TYPESAFE_API_KEY|$APP_FILE|$(who TYPESAFE_API_KEY)"
          "OPENAI_API_KEY|$APP_FILE|$(who OPENAI_API_KEY)")
  elif ! grep -Eq '^COMPOSE_PROFILES=.*worker' "$ENV_FILE"; then
    later+=("COMPOSE_PROFILES|$ENV_FILE|both AI keys are in: deploy/setup.sh worker on")
  fi

  # Any other line switched on in app.env must be a real value: the app refuses a placeholder.
  while IFS= read -r name; do
    case $name in OIDC_*|HOSTED_STORAGE_FILESYSTEM_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|TYPESAFE_API_KEY) continue ;; esac
    case $(state_of "$APP_FILE" "$name") in
      placeholder) blocking+=("$name|$APP_FILE|still the template's placeholder: $(who "$name"), or put # in front of it") ;;
      set) look "$APP_FILE" "$name" start ;;
    esac
  done < <(sed -nE 's/^([A-Z][A-Z0-9_]*)=.*/\1/p' "$APP_FILE")

  # Paddle's settings need each other, as the app checks at startup.
  if is_set "$APP_FILE" PADDLE_API_KEY; then
    is_set "$APP_FILE" PADDLE_WEBHOOK_SECRET || blocking+=("PADDLE_WEBHOOK_SECRET|$APP_FILE|PADDLE_API_KEY needs it: $(who PADDLE_WEBHOOK_SECRET)")
    is_set "$APP_FILE" PADDLE_CHECKOUT_URL || blocking+=("PADDLE_CHECKOUT_URL|$APP_FILE|PADDLE_API_KEY needs it: run deploy/setup.sh again")
    for name in OPERATOR_LEGAL_NAME OPERATOR_COUNTRY OPERATOR_CONTACT_EMAIL; do
      is_set "$APP_FILE" "$name" || blocking+=("$name|$APP_FILE|PADDLE_API_KEY needs it: $(who "$name")")
    done
  else
    is_set "$APP_FILE" PADDLE_CHECKOUT_URL && blocking+=("PADDLE_CHECKOUT_URL|$APP_FILE|needs PADDLE_API_KEY: paste the key, or put # in front of PADDLE_CHECKOUT_URL")
    is_set "$APP_FILE" PADDLE_CLIENT_TOKEN && blocking+=("PADDLE_CLIENT_TOKEN|$APP_FILE|needs PADDLE_API_KEY: $(who PADDLE_API_KEY)")
  fi
  if is_set "$APP_FILE" PADDLE_WEBHOOK_SECRET && ! is_set "$APP_FILE" PADDLE_PRICE_MONTHLY \
      && ! is_set "$APP_FILE" PADDLE_PRICE_YEARLY; then
    blocking+=("PADDLE_PRICE_MONTHLY|$APP_FILE|PADDLE_WEBHOOK_SECRET needs the price ids: $(who PADDLE_PRICE_MONTHLY)")
  fi

  # Needed later: the public pages and Paddle, then the backups.
  for name in OPERATOR_LEGAL_NAME OPERATOR_COUNTRY OPERATOR_CONTACT_EMAIL PADDLE_ENVIRONMENT \
              PADDLE_PRICE_MONTHLY PADDLE_PRICE_YEARLY PADDLE_WEBHOOK_SECRET PADDLE_API_KEY \
              PADDLE_CLIENT_TOKEN; do
    is_set "$APP_FILE" "$name" || later+=("$name|$APP_FILE|$(who "$name")")
  done
  for name in MOTHLI_BACKUP_S3_ENDPOINT MOTHLI_BACKUP_ACCESS_KEY_ID \
              MOTHLI_BACKUP_SECRET_ACCESS_KEY MOTHLI_BACKUP_ENCRYPTION_KEY; do
    look "$ENV_FILE" "$name" later
  done
  # The alerts (step 6a): the scheduled jobs report to the watcher once both are in.
  look "$ENV_FILE" MOTHLI_WATCH_URL later
  look "$ENV_FILE" MOTHLI_WATCH_TOKEN later
  # The chat's two keys come last, the writer last of all.
  later+=("${chat[@]+"${chat[@]}"}")

  print_group() {
    local title=$1; shift
    [ $# -gt 0 ] || return 0
    say "$title"
    local seen=" " entry n f w
    for entry in "$@"; do
      n=${entry%%|*}; f=${entry#*|}; w=${f#*|}; f=${f%%|*}
      case $seen in *" $n "*) continue ;; esac
      seen="$seen$n "
      printf '  %-32s %-15s %s\n' "$n" "$f" "$w"
    done
  }
  print_group "Still needed before Mothli can start:" "${blocking[@]+"${blocking[@]}"}"
  print_group "Needed later (Mothli starts without them):" "${later[@]+"${later[@]}"}"
  if [ ${#blocking[@]} -gt 0 ]; then
    say "Not ready to start: fill in the first list, then run deploy/setup.sh check again."
    exit 1
  fi
  say "Ready to start: nothing needed to start is missing."
}

# ---- backup-key: made by Mothli's own image, the one way the backups read it -------------------

cmd_backup_key() {
  local key
  [ -f "$ENV_FILE" ] || fail "$ENV_FILE does not exist yet: run deploy/setup.sh first"
  if [ "$(state_of "$ENV_FILE" MOTHLI_BACKUP_ENCRYPTION_KEY)" = set ]; then
    say "MOTHLI_BACKUP_ENCRYPTION_KEY is already set: kept. Never make a second one, a new key cannot open the backups made with the old one."
    return 0
  fi
  command -v docker >/dev/null 2>&1 || fail "docker is needed: the key is made by Mothli's own image"
  say "building Mothli's backend image if it is not built yet (the first time takes several minutes)"
  $COMPOSE build backup >&2
  key=$($COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups new-key) \
    || fail "making the backup key failed (the line above says why)"
  if ! [[ $key =~ ^AGE-SECRET-KEY-1[0-9A-Z]{50,}$ ]]; then
    key=
    fail "making the backup key gave something other than one key: nothing was written"
  fi
  write_line "$ENV_FILE" MOTHLI_BACKUP_ENCRYPTION_KEY "$key"
  key=
  say "MOTHLI_BACKUP_ENCRYPTION_KEY written to $ENV_FILE"
  $COMPOSE run --rm --no-deps -T backup python -m app.hosted.backups check-key
  say "Copy deploy/.env to your offline place again now: it holds the backup key."
}

# ---- cron: the three scheduled lines, marked as this script's own -------------------------------

MARK="# mothli-setup:"

cron_lines() {
  case $ROOT in
    *"'"*|*%*) fail "the checkout's path has ' or % in it, which a crontab line cannot hold: move it" ;;
  esac
  # Each job runs through deploy/report.sh, which passes its output to the log unchanged and tells
  # the watcher how it went (step 6a); without the two MOTHLI_WATCH_ settings it reports nothing.
  local compose="docker compose -f deploy/compose.yaml"
  say "0 3 * * * cd '$ROOT' && deploy/report.sh backup -- deploy/backup.sh >> deploy/backups/backup.log 2>&1 ${MARK}backup"
  say "0 4 * * * cd '$ROOT' && deploy/report.sh purge -- $compose run --rm purge >> deploy/backups/purge.log 2>&1 ${MARK}purge"
  say "30 * * * * cd '$ROOT' && deploy/report.sh paddle-check -- $compose exec -T web python -m app.hosted.paddle_check >> deploy/backups/paddle-check.log 2>&1 ${MARK}paddle-check"
}

cmd_cron() {
  local show=no current new
  case ${1:-} in
    --show) show=yes ;;
    "") ;;
    *) fail "unknown option $1 (deploy/setup.sh cron [--show])" ;;
  esac
  new=$(cron_lines)
  if [ $show = yes ]; then say "$new"; return 0; fi
  command -v crontab >/dev/null 2>&1 || fail "crontab is not installed (apt install cron)"
  current=$(crontab -l 2>/dev/null || true)
  mkdir -p deploy/backups
  {
    if [ -n "$current" ]; then printf '%s\n' "$current" | grep -vF "$MARK" || true; fi
    printf '%s\n' "$new"
  } | crontab -
  say "the crontab has Mothli's three lines (backup 03:00, erasing 04:00, Paddle check every hour at :30)"
  if printf '%s\n' "$current" | grep -vF "$MARK" \
      | grep -Eq 'deploy/backup\.sh|run --rm purge|app\.hosted\.paddle_check'; then
    say "note: the crontab also has an older Mothli line written by hand; remove it with crontab -e so nothing runs twice"
  fi
}

# ---- worker on|off: the one COMPOSE_PROFILES=worker line ----------------------------------------

cmd_worker() {
  local want=${1:-} state value tmp
  case $want in on|off) ;; *) fail "deploy/setup.sh worker on, or deploy/setup.sh worker off" ;; esac
  [ $# -le 1 ] || fail "deploy/setup.sh worker on, or deploy/setup.sh worker off"
  [ -f "$ENV_FILE" ] || fail "$ENV_FILE does not exist yet: run deploy/setup.sh first"
  state=$(state_of "$ENV_FILE" COMPOSE_PROFILES)
  value=$(value_of "$ENV_FILE" COMPOSE_PROFILES)
  if [ "$state" = set ] && [ "$value" != worker ]; then
    fail "COMPOSE_PROFILES in $ENV_FILE is set to something other than worker: change that line by hand"
  fi
  if [ "$want" = on ]; then
    if [ "$state" = set ]; then say "the job worker is already on"; return 0; fi
    [ "$(chat_keys)" = both ] || fail "the job worker needs both AI keys first: deploy/setup.sh set TYPESAFE_API_KEY, then deploy/setup.sh set OPENAI_API_KEY (or ANTHROPIC_API_KEY)"
    write_line "$ENV_FILE" COMPOSE_PROFILES worker
    say "the job worker is on; start it with: docker compose -f deploy/compose.yaml up -d --wait"
  else
    if [ "$state" != set ]; then say "the job worker is already off"; return 0; fi
    tmp=$(mktemp "$ENV_FILE.XXXXXX")
    awk '!done && /^COMPOSE_PROFILES=/ { print "# " $0; done = 1; next } { print }' "$ENV_FILE" > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$ENV_FILE"
    say "the job worker is off; stop it with: docker compose -f deploy/compose.yaml stop worker"
  fi
}

case ${1:-} in
  set) shift; cmd_set "$@" ;;
  worker) shift; cmd_worker "$@" ;;
  check) shift; cmd_check "$@" ;;
  backup-key) shift; cmd_backup_key "$@" ;;
  cron) shift; cmd_cron "$@" ;;
  -h|--help|help) usage ;;
  *) cmd_setup "$@" ;;
esac
