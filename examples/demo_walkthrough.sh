#!/usr/bin/env bash
# End-to-end walkthrough of the cross-chain message inbox using curl + jq.
#
# Scenario demonstrated:
#   1. Propose + confirm source blocks (chainA).
#   2. Submit signed messages out of order (seq1 then seq0); only messages
#      on FINAL blocks execute, advancing over the contiguous prefix.
#   3. Re-deliver the same envelope and observe exactly-once execution.
#   4. Produce a CONFLICTING signed copy of seq0 (equivocation): the stored
#      row is never overwritten, evidence is preserved, the channel freezes
#      and an alert is raised.
#
# Every message body is signed for real by cmd/signfixture (Ed25519); the
# server verifies signatures with the trusted public keys.
set -euo pipefail

BASE="${XIMBOX_BASE:-http://127.0.0.1:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
FIX="${HERE}/../bin/ximbox-signfixture"
[ -x "$FIX" ] || { echo "build the fixture first: make build"; exit 1; }

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
post() { curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' --data @"$2" | jq .; }

say "trusted signer registry"
"$FIX" keys | jq .

say "1. propose genesis + block 2 (chainA)"
GEN=$("$FIX" blockhash --chain chainA --height 1 \
  --parent 0x0000000000000000000000000000000000000000000000000000000000000000)
B2=$("$FIX" blockhash --chain chainA --height 2 --parent "$GEN")
printf 'genesis=%s\nblock2=%s\n' "$GEN" "$B2"
curl -sS -X POST "$BASE/v1/blocks" -H 'Content-Type: application/json' -d "$(jq -n \
  --arg c chainA --arg h "$GEN" \
  '{source_chain:$c,height:1,hash:$h,parent_hash:"0x0000000000000000000000000000000000000000000000000000000000000000"}')" | jq .
curl -sS -X POST "$BASE/v1/blocks" -H 'Content-Type: application/json' -d "$(jq -n \
  --arg c chainA --arg h "$B2" --arg p "$GEN" \
  '{source_chain:$c,height:2,hash:$h,parent_hash:$p}')" | jq .

say "2. confirm genesis; submit seq1 (on unconfirmed block2) -> staged"
curl -sS -X POST "$BASE/v1/blocks/confirm" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg c chainA --arg h "$GEN" '{source_chain:$c,hash:$h}')" | jq .
"$FIX" sign --chain chainA --channel walkthrough --seq 1 --block "$B2" \
  --body '{"type":"transfer","to":"bob","amount":25}' > /tmp/seq1.json
curl -sS -X POST "$BASE/v1/messages" -H 'Content-Type: application/json' --data @/tmp/seq1.json | jq .

say "3. submit seq0 on final genesis -> executes immediately"
"$FIX" sign --chain chainA --channel walkthrough --seq 0 --block "$GEN" \
  --body '{"type":"transfer","to":"alice","amount":100}' > /tmp/seq0.json
curl -sS -X POST "$BASE/v1/messages" -H 'Content-Type: application/json' --data @/tmp/seq0.json | jq .

say "4. confirm block2 -> staged seq1 executes (contiguous prefix)"
curl -sS -X POST "$BASE/v1/blocks/confirm" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg c chainA --arg h "$B2" '{source_chain:$c,hash:$h}')" | jq .

say "5. re-deliver seq0 twice -> still executed exactly once"
curl -sS -X POST "$BASE/v1/messages" -H 'Content-Type: application/json' --data @/tmp/seq0.json >/dev/null
curl -sS -X POST "$BASE/v1/messages" -H 'Content-Type: application/json' --data @/tmp/seq0.json >/dev/null

say "6. conflicting copy of seq0 (different body, validly signed) -> freeze + alert"
"$FIX" sign --chain chainA --channel walkthrough --seq 0 --block "$GEN" \
  --body '{"type":"transfer","to":"mallory","amount":999}' > /tmp/seq0_conflict.json
curl -sS -X POST "$BASE/v1/messages" -H 'Content-Type: application/json' \
  --data @/tmp/seq0_conflict.json | jq .

say "state: messages"
curl -sS "$BASE/v1/messages?source_chain=chainA&channel=walkthrough" | jq .
say "state: accounts (alice=100, bob=25, NO mallory)"
curl -sS "$BASE/v1/accounts" | jq .
say "state: evidence"
curl -sS "$BASE/v1/evidence?source_chain=chainA&channel=walkthrough" | jq .
say "state: alerts"
curl -sS "$BASE/v1/alerts?source_chain=chainA&channel=walkthrough" | jq .

echo
echo "walkthrough complete."
