#!/usr/bin/env bash
# Reproducible end-to-end demonstration of the bse schema-evolution codec.
#
# Usage:
#   ./examples/demo.sh            # uses ./target/debug/bse
#   BSE=/path/to/bse ./examples/demo.sh
#
# Requires: a built `bse` binary and `jq` (for composing JSON requests).
# Prints each step and exits non-zero on the first failure.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BSE="${BSE:-$ROOT/target/debug/bse}"
V1="$HERE/schema_person_v1.json"
V2="$HERE/schema_person_v2.json"
V3="$HERE/schema_person_v3_incompatible.json"

[ -x "$BSE" ] || { echo "build first: cargo build"; exit 2; }
command -v jq >/dev/null || { echo "jq is required"; exit 2; }

step() { printf '\n========== %s ==========\n' "$1"; }

# Generate the self-contained request samples from the schema files.
python3 "$HERE/build_requests.py" >/dev/null

step "1. encode under v1 (id explicitly 0, email missing)"
ENC1=$("$BSE" -f "$HERE/req_encode_v1.json")
echo "$ENC1" | jq -c '.result | {payload_bytes, data_hex}'
B64V1=$(echo "$ENC1" | jq -r .result.data_b64)

step "2. decode v1 bytes under v1: present 0 vs missing email"
jq -n --arg b64 "$B64V1" --slurpfile s "$V1" \
  '{op:"decode",schema:$s[0],data_b64:$b64}' | "$BSE" --pretty \
  | jq '.result.message | {id, email, active}'

step "3. encode under v2 (adds age/scores/nickname/city)"
B64V2=$("$BSE" -f "$HERE/req_encode_v2.json" | jq -r .result.data_b64)
echo "v2 envelope base64 (first 48 chars): ${B64V2:0:48}..."

step "4. read NEW (v2) data with OLD (v1) schema -> unknown fields captured"
jq -n --arg b64 "$B64V2" --slurpfile s "$V1" \
  '{op:"decode",schema:$s[0],data_b64:$b64}' | "$BSE" --pretty \
  | jq '.result | {known_name: .message.name, unknown: .message.__unknown__, stats}'

step "5. forward through v1 (patch name), then read under v2 again"
FWD=$(jq -n --arg b64 "$B64V2" --slurpfile s "$V1" \
  '{op:"forward",schema:$s[0],data_b64:$b64,"set":{"name":"Edited by v1"}}' | "$BSE")
B64F=$(echo "$FWD" | jq -r .result.data_b64)
jq -n --arg b64 "$B64F" --slurpfile s "$V2" \
  '{op:"decode",schema:$s[0],data_b64:$b64}' | "$BSE" --pretty \
  | jq '.result.message | {name, age, scores, nickname, city: .addr.present.city}'

step "6. read OLD (v1) data with NEW (v2) schema -> added fields absent"
jq -n --arg b64 "$B64V1" --slurpfile s "$V2" \
  '{op:"decode",schema:$s[0],data_b64:$b64}' | "$BSE" --pretty \
  | jq '.result.message | {id, age, scores}'

step "7. compatibility: v1 vs v2 (compatible)"
"$BSE" -f "$HERE/req_compat_v1_v2.json" | jq -c '.result.compatible'

step "8. compatibility: v1 vs v3 (string->int32 + added required) REJECTED"
jq -n --slurpfile o "$V1" --slurpfile n "$V3" \
  '{op:"compat",old_schema:$o[0],new_schema:$n[0]}' | "$BSE" --pretty \
  | jq '(.result.compatible), (.result.reports[] | select(.compatible==false) | .fields[] | select(.verdict=="incompatible"))'

step "9. actual decode of v2 bytes under v3 is rejected (typed error)"
set +e
jq -n --arg b64 "$B64V2" --slurpfile s "$V3" \
  '{op:"decode",schema:$s[0],data_b64:$b64}' | "$BSE"
echo "exit code = $?  (non-zero is expected)"
set -e

step "10. truncation: payload cut to 20 bytes -> clean error, no panic"
CUT=$(jq -n --arg b64 "$B64V1" '{op:"truncate",data_b64:$b64,"keep":20}' | "$BSE" | jq -r .result.data_b64)
set +e
jq -n --arg b64 "$CUT" --slurpfile s "$V1" \
  '{op:"decode",schema:$s[0],data_b64:$b64}' | "$BSE"
echo "exit code = $?  (non-zero is expected)"
set -e

printf '\nAll demo steps completed.\n'
