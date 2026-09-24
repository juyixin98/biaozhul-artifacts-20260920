#!/usr/bin/env bash
# failure-cases.sh — demonstrates the atomicity guarantees. Run after
# demo.sh (uses its state: staging on v1, v2 in history).
set -uo pipefail
BASE="${BASE:-http://localhost:8080}"
BLOB_ROOT="${BLOB_ROOT:-../data/blobs}"
cd "$(dirname "$0")"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

env_gen()  { curl -sf "$BASE/v1/envs/$1" | jq -r '.generation'; }
env_cur()  { curl -sf "$BASE/v1/envs/$1" | jq -r '.current_digest'; }
# add_evidence DIGEST -> version
add_evidence() {
  jq --arg d "$1" '.artifact_digest=$d' evidence.json \
    | curl -sf -XPOST "$BASE/v1/evidence" -d @- | jq -r '.version'
}
# approve DIGEST -> approval uuid
approve() {
  jq --arg d "$1" '.artifact_digest=$d' approval.json \
    | curl -sf -XPOST "$BASE/v1/approvals" -d @- | jq -r '.id'
}
# promote DIGEST EVIDENCE_VERSION APPROVAL EXPECTED_GEN
promote() {
  jq --arg d "$1" --argjson ev "$2" --arg a "$3" --argjson g "$4" \
    '.digest=$d | .evidence_version=$ev | .approval_id=$a | .expected_generation=$g' promotion.json \
    | curl -s -XPOST "$BASE/v1/promotions" -d @-
}

say "1. stale expected_generation → 409 generation_conflict"
D=$(env_cur test)
EV=$(add_evidence "$D")
A=$(approve "$D")
promote "$D" "$EV" "$A" 999 | jq '{status, failure_reason}'

say "2. concurrent promotions, same expected_generation → exactly one wins"
G_TEST=$(env_gen test)
D2=$(curl -sf -XPOST "$BASE/v1/envs/test/artifacts?expected_generation=$G_TEST" \
  --data-binary "payment-svc build 3" | jq -r '.artifact_digest')
EV2=$(add_evidence "$D2")
A1=$(approve "$D2"); A2=$(approve "$D2")
G=$(env_gen staging)
promote "$D2" "$EV2" "$A1" "$G" | jq -c '{status, failure_reason}' &
promote "$D2" "$EV2" "$A2" "$G" | jq -c '{status, failure_reason}' &
wait

say "3. copy failure → attempt failed, pointer untouched"
BEFORE=$(env_gen staging)
G_TEST=$(env_gen test)
D3=$(curl -sf -XPOST "$BASE/v1/envs/test/artifacts?expected_generation=$G_TEST" \
  --data-binary "payment-svc build 4" | jq -r '.artifact_digest')
rm -f "$BLOB_ROOT/test/${D3#sha256:}"   # destroy the source blob
EV3=$(add_evidence "$D3")
A3=$(approve "$D3")
promote "$D3" "$EV3" "$A3" "$(env_gen staging)" | jq '{status, failure_reason}'
AFTER=$(env_gen staging)
echo "staging generation before=$BEFORE after=$AFTER (unchanged ⇒ old version still live)"

say "4. late approval → 409 late_approval"
# Approve against the current staging generation, move staging forward with
# a rollback, then try to use the now-stale approval.
D4=$(env_cur test)
A4=$(approve "$D4")
D_V2=$(curl -sf "$BASE/v1/envs/staging/history" | jq -r '[.[] | select(.current|not)][0].digest')
jq --arg d "$D_V2" --argjson g "$(env_gen staging)" \
  '.digest=$d | .expected_generation=$g' rollback.json \
  | curl -s -XPOST "$BASE/v1/rollbacks" -d @- | jq -c '{kind, status}'
EV4=$(add_evidence "$D4")
promote "$D4" "$EV4" "$A4" "$(env_gen staging)" | jq '{status, failure_reason}'

say "5. every attempt's evidence is persisted"
curl -sf "$BASE/v1/attempts" | jq '.[] | {kind, status, failure_reason, digest: .artifact_digest[:19]}'
