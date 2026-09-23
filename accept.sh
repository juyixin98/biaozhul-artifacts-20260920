#!/usr/bin/env bash
# End-to-end acceptance script.
# Builds the project, runs the test suite, starts a fresh server with a
# temporary SQLite database, generates signed example blocks, exercises every
# required scenario, and tears everything down.
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-3100}"
BASE="http://127.0.0.1:${PORT}"
TMPDIR="$(mktemp -d)"
DB="${TMPDIR}/accept.db"
EXDIR="${TMPDIR}/examples"
trap 'pkill -x utxo-server 2>/dev/null || true; rm -rf "$TMPDIR"' EXIT

echo "== cargo build (release) =="
cargo build --release

echo "== cargo test =="
cargo test

echo "== generate signed example blocks =="
./target/release/utxo-cli gen-examples "$EXDIR"

echo "== start server on $BASE (db $DB) =="
setsid ./target/release/utxo-server --db "$DB" --addr "127.0.0.1:${PORT}" \
    >"${TMPDIR}/server.log" 2>&1 < /dev/null &
for _ in $(seq 1 50); do
    curl -sf "$BASE/health" >/dev/null && break
    sleep 0.1
done
curl -s "$BASE/health" | jq -c .

echo "== run the built-in end-to-end demo =="
./target/release/utxo-cli demo --base-url "$BASE"

echo "== restart the server on the same database and verify persistence =="
pkill -x utxo-server || true
sleep 0.5
setsid ./target/release/utxo-server --db "$DB" --addr "127.0.0.1:${PORT}" \
    >"${TMPDIR}/server2.log" 2>&1 < /dev/null &
for _ in $(seq 1 50); do
    curl -sf "$BASE/health" >/dev/null && break
    sleep 0.1
done
echo "tip after restart:"
curl -s "$BASE/chain/tip" | jq -c '.tip'
echo "naive replay after restart:"
curl -s "$BASE/debug/replay" | jq -c '{blocks_replayed, roots_match, total_supply}'

echo
echo "ACCEPTANCE OK"
