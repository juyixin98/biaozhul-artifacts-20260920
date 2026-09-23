#!/usr/bin/env bash
# Demonstrates a pre-commit crash and exact-once recovery across restarts.
#
# It launches the REAL server binary with XIMBOX_CRASH_AT armed, waits for it
# to exit(99) during startup recovery, inspects rolled-back state, then starts
# a clean process and shows the contiguous prefix completed exactly once.
#
# The script resets the LOCAL demo database at start (all ximbox tables).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="${ROOT}/bin/ximbox-server"
FIX="${ROOT}/bin/ximbox-signfixture"
[ -x "$BIN" ] && [ -x "$FIX" ] || { echo "run 'make build' first"; exit 1; }

export XIMBOX_DATABASE_URL="${XIMBOX_DATABASE_URL:-postgres://ximbox:ximbox_dev_pwd@localhost:5432/ximbox?sslmode=disable}"
# psql connection parts (default local dev setup; override via env if needed).
PGHOST="${XIMBOX_PGHOST:-localhost}"
PGUSER="${XIMBOX_PGUSER:-ximbox}"
PGDATABASE="${XIMBOX_PGDATABASE:-ximbox}"
export PGPASSWORD="${XIMBOX_PGPASSWORD:-ximbox_dev_pwd}"

PORT="${XIMBOX_PORT:-18080}"
CHAIN="chainA"
CHAN="crash-demo"
ZERO="0x0000000000000000000000000000000000000000000000000000000000000000"
LOGDIR="$(mktemp -d)"
echo "logs in $LOGDIR"

say() { printf '\n\033[1;35m== %s ==\033[0m\n' "$*"; }

sql() { psql -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -v ON_ERROR_STOP=1 "$@"; }

jpost() { # jpost PATH JSON
  curl -fsS -X POST "http://127.0.0.1:${PORT}$1" -H 'Content-Type: application/json' -d "$2"
}

start_server() { # start_server LOGFILE [extra env assignments...]
  local log="$1"; shift
  env "$@" XIMBOX_HTTP_ADDR="127.0.0.1:${PORT}" \
    "$BIN" >"$log" 2>&1 &
  echo $!
}

wait_http_up() {
  for _ in $(seq 1 150); do
    curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null && return 0
    sleep 0.1
  done
  echo "server did not become healthy" >&2
  return 1
}

wait_http_down() {
  for _ in $(seq 1 100); do
    curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null || return 0
    sleep 0.1
  done
  return 1
}

say "reset local demo database"
sql -c "TRUNCATE executions, accounts, alerts, message_evidence, messages, blocks, channels RESTART IDENTITY CASCADE" >/dev/null

say "prepare: confirmed chain, seq0 executed, seq1 staged on a final block"
PID=$(start_server "$LOGDIR/prep.log")
trap 'kill "$PID" 2>/dev/null || true' EXIT
wait_http_up

GEN=$("$FIX" blockhash --chain "$CHAIN" --height 1 --parent "$ZERO")
B2=$("$FIX" blockhash --chain "$CHAIN" --height 2 --parent "$GEN")
jpost /v1/blocks "$(jq -n --arg h "$GEN" \
  '{source_chain:"chainA",height:1,hash:$h,parent_hash:"'$ZERO'"}')" >/dev/null
jpost /v1/blocks/confirm "$(jq -n --arg h "$GEN" '{source_chain:"chainA",hash:$h}')" >/dev/null
jpost /v1/blocks "$(jq -n --arg h "$B2" --arg p "$GEN" \
  '{source_chain:"chainA",height:2,hash:$h,parent_hash:$p}')" >/dev/null
"$FIX" sign --chain "$CHAIN" --channel "$CHAN" --seq 0 --block "$GEN" \
  --body '{"type":"transfer","to":"alice","amount":100}' |
  jpost /v1/messages @- >/dev/null
"$FIX" sign --chain "$CHAIN" --channel "$CHAN" --seq 1 --block "$B2" \
  --body '{"type":"transfer","to":"bob","amount":25}' |
  jpost /v1/messages @- >/dev/null

# seq1 must be staged pending because block 2 is still proposed.
[ "$(sql -tAc "SELECT status FROM messages WHERE channel_id='${CHAN}' AND sequence=1")" = "pending" ] \
  || { echo "precondition failed: seq1 not pending"; exit 1; }

# Finalize block 2 DIRECTLY, bypassing the service advancement: reproduces a
# confirmed-but-not-yet-processed message just before the crash run starts.
sql -c "UPDATE blocks SET status='final', finalized_at=now() WHERE hash='${B2}'" >/dev/null
kill "$PID"; wait "$PID" 2>/dev/null || true
wait_http_down
trap - EXIT

say "run A: crash armed on sequence 1 (expect exit code 99 during recovery)"
set +e
env XIMBOX_HTTP_ADDR="127.0.0.1:${PORT}" \
    XIMBOX_DATABASE_URL="$XIMBOX_DATABASE_URL" \
    XIMBOX_CRASH_AT="source_chain=${CHAIN},channel=${CHAN},sequence=1" \
    "$BIN" >"$LOGDIR/crash.log" 2>&1
EC=$?
set -e
echo "exit code=$EC (want 99)"
[ "$EC" = "99" ] || { echo "unexpected exit; log:"; cat "$LOGDIR/crash.log"; exit 1; }
grep -q 'CRASH INJECTION' "$LOGDIR/crash.log" && echo "crash marker present in log"
wait_http_down

say "durable state with process dead: seq1 transaction rolled back"
sql -c "SELECT sequence,status FROM messages WHERE source_chain='${CHAIN}' AND channel_id='${CHAN}' ORDER BY sequence"
sql -c "SELECT address,balance FROM accounts ORDER BY address"
sql -c "SELECT COUNT(*) AS executions_ledger_rows FROM executions WHERE sequence=1"
[ "$(sql -tAc "SELECT status FROM messages WHERE channel_id='${CHAN}' AND sequence=1")" = "pending" ] \
  || { echo "seq1 must remain pending"; exit 1; }
[ "$(sql -tAc "SELECT COALESCE((SELECT balance FROM accounts WHERE address='bob'),0)")" = "0" ] \
  || { echo "bob must have no balance after rollback"; exit 1; }

say "run B: clean restart -> recovery completes seq1 exactly once"
PID=$(start_server "$LOGDIR/recover.log")
trap 'kill "$PID" 2>/dev/null || true' EXIT
wait_http_up
sleep 0.5
curl -fsS "http://127.0.0.1:${PORT}/v1/messages?source_chain=${CHAIN}&channel=${CHAN}" | jq '.messages[] | {seq:.sequence,status:.status}'
curl -fsS "http://127.0.0.1:${PORT}/v1/accounts" | jq '.accounts[]'
[ "$(sql -tAc "SELECT status FROM messages WHERE channel_id='${CHAN}' AND sequence=1")" = "executed" ]
[ "$(sql -tAc "SELECT balance FROM accounts WHERE address='alice'")" = "100" ]
[ "$(sql -tAc "SELECT balance FROM accounts WHERE address='bob'")" = "25" ]

say "run C: another restart -> idempotent, balances unchanged"
kill "$PID"; wait "$PID" 2>/dev/null || true
wait_http_down
trap - EXIT
PID=$(start_server "$LOGDIR/recover2.log")
trap 'kill "$PID" 2>/dev/null || true' EXIT
wait_http_up
sleep 0.5
[ "$(sql -tAc "SELECT balance FROM accounts WHERE address='alice'")" = "100" ]
[ "$(sql -tAc "SELECT balance FROM accounts WHERE address='bob'")" = "25" ]
sql -c "SELECT COUNT(*) AS seq1_execution_rows FROM executions WHERE sequence=1"
kill "$PID"; wait "$PID" 2>/dev/null || true
trap - EXIT

echo
echo "crash/recovery demo complete; logs: $LOGDIR"
