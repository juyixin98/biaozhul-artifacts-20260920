#!/usr/bin/env bash
# End-to-end acceptance walkthrough for the Merkle State Proof Service.
#
# Starts the server on an ephemeral data directory, writes two batches,
# fetches existence + non-existence proofs, and submits every proof to the
# /v1/verify endpoint. Requires: curl, jq, od (coreutils), and a built binary
# (`cargo build`).
set -euo pipefail

DB_DIR="${DB_DIR:-$(mktemp -d -t merkle-demo-XXXXXX)}"
ADDR="${ADDR:-127.0.0.1:39173}"
BIN="${BIN:-./target/debug/merkle-proof-service}"

hex()  { printf '%s' "$1" | xxd -p -c 1000000; }

echo "==> starting server (db: $DB_DIR, addr: $ADDR)"
"$BIN" --db "$DB_DIR" --addr "$ADDR" >/tmp/merkle-demo.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

# Wait for the listener.
READY=0
for _ in $(seq 1 50); do
  if curl -fsS "http://$ADDR/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 0.1
done
if [ "$READY" != "1" ]; then
  echo "server failed to start; log:" >&2
  cat /tmp/merkle-demo.log >&2
  exit 1
fi

post() { curl -fsS -X POST -H 'content-type: application/json' "$@"; }
get()  { curl -fsS "$@"; }

echo
echo "==> empty-tree root (version 0)"
get "http://$ADDR/v1/roots/0"

echo; echo "==> batch 1: alpha=100, bob=200, carol=300, delta=<empty>, dup last-write-wins"
R1=$(post "http://$ADDR/v1/batches" --data @<(jq '.["01_first_batch"]' examples/batches.json))
echo "$R1" | jq .
ROOT1=$(echo "$R1" | jq -r .root)

echo; echo "==> existence proof for 'bob' under root1"
P_BOB=$(get "http://$ADDR/v1/proofs/key/$(hex bob)")
echo "$P_BOB" | jq '{exists, key, value, index: .proof.index, leaf_count: .proof.leaf_count, path_len: (.proof.path|length)}'
echo "==> verify it against root1"
post "http://$ADDR/v1/verify" -d "$(jq -n --arg r "$ROOT1" --argjson p "$P_BOB" '{root:$r, response:$p}')"

echo; echo "==> non-existence proof for 'bruce' (between bob and carol) under root1"
P_MISS=$(get "http://$ADDR/v1/proofs/key/$(hex bruce)")
echo "$P_MISS" | jq '{exists, bounds: [.proof.bounds[] | {side, neighbor: .proof.entry.key, index: .proof.index}]}'
post "http://$ADDR/v1/verify" -d "$(jq -n --arg r "$ROOT1" --argjson p "$P_MISS" '{root:$r, response:$p}')"

echo; echo "==> tamper with bob's value -> verification must fail"
BAD=$(echo "$P_BOB" | jq '.value="deadbeef" | .proof.entry.value="deadbeef"')
post "http://$ADDR/v1/verify" -d "$(jq -n --arg r "$ROOT1" --argjson p "$BAD" '{root:$r, response:$p}')"

echo; echo "==> edge non-existence: key below minimum ('aaa') and above maximum ('zzz')"
for k in aaa zzz; do
  P=$(get "http://$ADDR/v1/proofs/key/$(hex "$k")")
  echo "  $k bounds: $(echo "$P" | jq -c '[.proof.bounds[] | .side]')"
  post "http://$ADDR/v1/verify" -d "$(jq -n --arg r "$ROOT1" --argjson p "$P" '{root:$r, response:$p}')" >/dev/null
done

echo; echo "==> batch 2: alpha=101, delete carol"
R2=$(post "http://$ADDR/v1/batches" --data @<(jq '.["02_update_and_delete"]' examples/batches.json))
echo "$R2" | jq '{version, root, leaf_count}'
ROOT2=$(echo "$R2" | jq -r .root)

echo; echo "==> history: carol EXISTS at v1 but is MISSING at v2"
get "http://$ADDR/v1/proofs/key/$(hex carol)?version=1" | jq '{version: .proof.version, exists}'
get "http://$ADDR/v1/proofs/key/$(hex carol)?version=2" | jq '{version: .proof.version, exists}'

echo; echo "==> old proof (root1 era) must NOT verify under root2"
post "http://$ADDR/v1/verify" -d "$(jq -n --arg r "$ROOT2" --argjson p "$P_BOB" '{root:$r, response:$p}')"

echo; echo "==> version history"
get "http://$ADDR/v1/versions" | jq '.versions[] | {version, leaf_count}'

echo; echo "ALL DEMO STEPS COMPLETED"
