#!/usr/bin/env bash
# Local Docker smoke test: builds the image and runs the real compose.yaml in
# isolation (own project, container name, image tag and a temporary data
# folder), then checks startup, persistence, restart recovery, backup/restore
# and the security basics. Safe to repeat; never touches runtime/ or data/.
#
# Needs a separate TEST bot (from @BotFather) whose token is in runtime/test.env
# (or SMOKE_ENV_FILE). Nobody else talks to that bot, and Gmail is switched
# off, so the test can't message real users.
#
# Usage: make docker-smoke-test     (exit code 0 = every check passed)

set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

TEST_ENV="${SMOKE_ENV_FILE:-runtime/test.env}"
if [[ ! -f "$TEST_ENV" ]]; then
  echo "Missing $TEST_ENV: create a test bot with @BotFather and put TELEGRAM_BOT_TOKEN=… and"
  echo "TELEGRAM_ALLOWLIST=[<your id>] there (plus LLM_API_KEY if you want to chat with it)."
  exit 2
fi

token_hash() {  # sha256 of TELEGRAM_BOT_TOKEN in an env file, empty if none (the token is never printed)
  [[ -f "$1" ]] || return 0
  sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$1" | tail -n1 | tr -d '"'"'"' \r' | { read -r t; [[ -n "${t:-}" ]] && printf '%s' "$t" | sha256sum | cut -d' ' -f1; }
}
TEST_HASH="$(token_hash "$TEST_ENV")"
if [[ -z "$TEST_HASH" ]]; then
  echo "$TEST_ENV has no TELEGRAM_BOT_TOKEN."
  exit 2
fi
if [[ "$(token_hash .env)" == "$TEST_HASH" ]]; then  # .env holds the real bot on the laptop
  echo "Refusing to run: $TEST_ENV uses the same bot token as .env. Use a separate test bot."
  exit 2
fi

mkdir -p .cache
WORK="$(mktemp -d "$REPO/.cache/smoke.XXXXXX")"  # inside the repo: Docker Desktop shares $HOME only
case "$WORK" in "$REPO/runtime"*|"$REPO/data"*) echo "Refusing to use $WORK"; exit 2;; esac
mkdir -p "$WORK/data" "$WORK/secrets" "$WORK/restore"
chmod 700 "$WORK/data" "$WORK/secrets"
{
  grep -v -E '^(DRY_RUN|KILL_SWITCH|GMAIL_ENABLED)=' "$TEST_ENV"
  echo "DRY_RUN=true"
  echo "KILL_SWITCH=true"
  echo "GMAIL_ENABLED=false"
} > "$WORK/test.env"
chmod 600 "$WORK/test.env"

export COMPOSE_PROJECT_NAME=offer-finder-smoke
export OFFER_FINDER_CONTAINER=offer-finder-smoke-bot
export IMAGE_TAG=smoke
export OFFER_FINDER_ENV_FILE="$WORK/test.env"
export OFFER_FINDER_DATA_DIR="$WORK/data"
export OFFER_FINDER_SECRETS_DIR="$WORK/secrets"
export APP_UID="$(id -u)" APP_GID="$(id -g)"
CONTAINER="$OFFER_FINDER_CONTAINER"
dc() { docker compose "$@"; }

cleanup() {
  dc down --remove-orphans >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

PASSED=() FAILED=()
check() {  # check <name> <command...>
  local name="$1"; shift
  if "$@"; then PASSED+=("$name"); echo "PASS  $name"; else FAILED+=("$name"); echo "FAIL  $name"; fi
}
in_bot() { dc exec -T bot "$@"; }
health() { docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null; }
wait_healthy() {
  for _ in $(seq 1 60); do [[ "$(health)" == healthy ]] && return 0; sleep 2; done
  return 1
}
no_traceback() { ! dc logs bot 2>&1 | grep -q Traceback; }
one_container() { [[ "$(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" | wc -l)" -eq 1 ]]; }

MARKER="smoke-$(date +%s)"
write_marker() {
  in_bot python -c "import sqlite3; c = sqlite3.connect('/data/offer_finder.db'); \
c.execute('CREATE TABLE IF NOT EXISTS _smoke (v TEXT)'); c.execute('INSERT INTO _smoke VALUES (?)', ('$MARKER',)); c.commit()"
}
MARKER_QUERY="import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); \
sys.exit(0 if c.execute('SELECT 1 FROM _smoke WHERE v = ?', (sys.argv[2],)).fetchone() else 1)"
live_has_marker() { in_bot python -c "$MARKER_QUERY" /data/offer_finder.db "$MARKER"; }  # WAL: read it inside
file_has_marker() { uv run --quiet python -c "$MARKER_QUERY" "$1" "$MARKER"; }  # a closed backup copy
verify_host() { uv run --quiet python -m offer_finder.cli verify-db "$1" >/dev/null; }

kill_bot_process() {  # a crash from inside (docker stop/kill would count as a manual stop)
  in_bot python -c "import os, signal
for pid in filter(str.isdigit, os.listdir('/proc')):
    if int(pid) != os.getpid() and b'offer_finder.bot' in open(f'/proc/{pid}/cmdline', 'rb').read():
        os.kill(int(pid), signal.SIGKILL)" >/dev/null 2>&1
  return 0
}
restarted_healthy() {
  local before="$1"
  for _ in $(seq 1 60); do
    local count; count="$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)"
    [[ "$count" -gt "$before" && "$(health)" == healthy ]] && return 0
    sleep 2
  done
  return 1
}

backup_and_restore() {
  local inside; inside="$(in_bot python -m offer_finder.cli backup | tail -n1 | tr -d '\r')" || return 1
  local host="$WORK/data/${inside#/data/}"
  [[ -f "$host" ]] || return 1
  cp "$host" "$WORK/restore/restored.db"
  verify_host "$WORK/restore/restored.db" && file_has_marker "$WORK/restore/restored.db"
}

not_root() { [[ "$(in_bot id -u | tr -d '\r')" != "0" ]]; }
image_has_no_secrets() {
  local found
  found="$(docker run --rm --entrypoint sh "offer-finder:$IMAGE_TAG" -c \
    'find / -xdev \( -name ".env*" -o -name "*.db" -o -name "gmail_*.json" \) 2>/dev/null')"
  [[ -z "$found" ]] && ! docker history --no-trunc "offer-finder:$IMAGE_TAG" | grep -q TELEGRAM_BOT_TOKEN
}
no_published_ports() { [[ -z "$(docker port "$CONTAINER" 2>/dev/null)" ]]; }

echo "== offline tests first"
uv run --quiet pytest -q tests/storage tests/bot tests/test_cli.py >/dev/null 2>&1
check "pytest (storage, bot, cli)" test $? -eq 0

echo "== 1. build and start"
check "image builds" dc build --quiet
check "container becomes healthy" dc up -d --wait --wait-timeout 120
check "no Traceback in the logs" no_traceback
check "log file is written" test -s "$WORK/data/logs/offer_finder.log"

echo "== 2. persistence"
check "write a marker row" write_marker
check "recreate the container" dc up -d --force-recreate --wait --wait-timeout 120
check "marker survives the recreate" live_has_marker
check "verify-db after the recreate" in_bot python -m offer_finder.cli verify-db /data/offer_finder.db

echo "== 3. restart recovery"
RESTARTS="$(docker inspect -f '{{.RestartCount}}' "$CONTAINER")"
kill_bot_process
check "back to healthy after a crash" restarted_healthy "$RESTARTS"
check "still exactly one container" one_container
check "no Traceback after the restart" no_traceback

echo "== 4. backup and restore"
check "backup restores into a separate folder" backup_and_restore

echo "== 5. security basics"
check "runs as a non-root user" not_root
check "image holds no .env, database or Gmail files" image_has_no_secrets
check "no published ports" no_published_ports

echo
echo "${#PASSED[@]} passed, ${#FAILED[@]} failed"
if (( ${#FAILED[@]} )); then
  printf '  failed: %s\n' "${FAILED[@]}"
  echo "Last log lines (bot tokens masked):"
  dc logs --tail=30 bot 2>&1 | sed -E 's/[0-9]{5,}:[A-Za-z0-9_-]{8,}/<token>/g; s/^/  /'
  exit 1
fi
