#!/usr/bin/env bash
# End-to-end acceptance test for the UTXO rollback validator.
#
# It builds the server, starts it on a real TCP port backed by a fresh SQLite
# file, drives every scenario over HTTP (valid chain, intra-block double
# spend, negative amount, overspend, unknown input, bad coinbase, bad linkage,
# consecutive disconnect, illegal + legal alternative blocks, naive-replay
# cross-check, explicit-height history, and restart persistence), then kills
# the server. Exits non-zero on the first failed expectation.
set -euo pipefail

cd "$(dirname "$0")"

# Pick an ephemeral free port (avoid colliding with other local services).
PORT="$(python3 -c 'import socket
s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
ADDR="127.0.0.1:${PORT}"
BASE="http://${ADDR}"
WORK="$(mktemp -d)"
DB="${WORK}/accept.db"
LOG="${WORK}/server.log"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "${WORK}"' EXIT

# Field extractor: jget "['key']" (python-style path). Emits JSON so bools
# are `true`/`false` and arrays stay valid JSON.
jget() { python3 -c 'import json,sys
path = sys.argv[1]
v = json.load(sys.stdin)
for key in path.strip("[]'\''").split("']['"):
    if key == "":
        continue
    v = v[key]
if isinstance(v, (dict, list, bool)):
    json.dump(v, sys.stdout)
else:
    print(v)' "$1"; }

PASS=0
FAIL=0
ok() { PASS=$((PASS+1)); echo "  PASS: $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $*"; echo "  response: ${BODY:-}"; }

# expect_status EXPECTED HTTP_STATUS LABEL
expect_status() {
  local want="$1" got="$2" label="$3"
  if [[ "${got}" == "${want}" ]]; then ok "${label} (HTTP ${got})"; else bad "${label}: want ${want}, got ${got}"; fi
}
# expect_field JSON FIELD EXPECTED LABEL
expect_field() {
  local json="$1" field="$2" want="$3" label="$4"
  local got
  got="$(printf '%s' "${json}" | jget "['${field}']")"
  if [[ "${got}" == "${want}" ]]; then ok "${label}: ${field}=${want}"; else bad "${label}: ${field} want '${want}' got '${got}'"; fi
}
# compare a response field to examples/expected.json
expect_known() {
  local json="$1" field="$2" key="$3" label="$4"
  local want got
  want="$(python3 -c "import json;print(json.load(open('examples/expected.json'))['${key}'])")"
  got="$(printf '%s' "${json}" | jget "['${field}']")"
  if [[ "${got}" == "${want}" ]]; then ok "${label}: ${field} matches ${key}"; else bad "${label}: ${field} want '${want}' got '${got}'"; fi
}

req() { # METHOD PATH [JSONFILE | -]
  local method="$1" path="$2" file="${3:-}"
  if [[ -n "${file}" ]]; then
    curl -sS -X "${method}" -H 'content-type: application/json' \
      --data @"${file}" "${BASE}${path}" -w $'\n%{http_code}'
  else
    curl -sS -X "${method}" "${BASE}${path}" -w $'\n%{http_code}'
  fi
}
reqraw() { # METHOD PATH JSON-STRING
  curl -sS -X "$1" -H 'content-type: application/json' --data "$3" "${BASE}$2" -w $'\n%{http_code}'
}
split_resp() { BODY="${1%$'\n'*}"; STATUS="${1##*$'\n'}"; }

echo "== Building (cargo build) =="
cargo build --quiet

echo "== Starting server (UTXO_DB=${DB}) =="
UTXO_DB="${DB}" UTXO_ADDR="${ADDR}" ./target/debug/utxo-rollback >"${LOG}" 2>&1 &
SERVER_PID=$!
READY=""
for _ in $(seq 1 50); do
  if curl -sS "${BASE}/health" 2>/dev/null | grep -q '"status":"ok"'; then READY=1; break; fi
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "server exited early; log:"; cat "${LOG}"; exit 1
  fi
  sleep 0.2
done
[[ -n "${READY}" ]] || { echo "server did not become ready; log:"; cat "${LOG}"; exit 1; }
H="$(curl -sS "${BASE}/health")"
expect_field "${H}" status ok "health"


echo "== Build canonical chain to height 2 =="
split_resp "$(req POST /blocks examples/block1.json)"
expect_status 201 "${STATUS}" "accept block1"
expect_known "${BODY}" hash block1.hash "block1"
split_resp "$(req POST /blocks examples/block2.json)"
expect_status 201 "${STATUS}" "accept block2"
expect_known "${BODY}" hash block2.hash "block2"

echo "== Every invalid block3 is rejected as a WHOLE block (tip stays 2) =="
split_resp "$(req POST /blocks examples/block3-doublespend.json)"
expect_status 400 "${STATUS}" "reject block3-doublespend"
expect_field "${BODY}" error DOUBLE_SPEND "double-spend error code"
split_resp "$(req POST /blocks examples/block3-negative.json)"
expect_status 400 "${STATUS}" "reject block3-negative"
expect_field "${BODY}" error NEGATIVE_AMOUNT "negative-amount error code"
split_resp "$(req POST /blocks examples/block3-overspend.json)"
expect_status 400 "${STATUS}" "reject block3-overspend"
expect_field "${BODY}" error INSUFFICIENT_INPUTS "overspend error code"
split_resp "$(req POST /blocks examples/block3-unknown-input.json)"
expect_status 400 "${STATUS}" "reject block3-unknown-input"
expect_field "${BODY}" error UNKNOWN_INPUT "unknown-input error code"
split_resp "$(req POST /blocks examples/block3-bad-reward.json)"
expect_status 400 "${STATUS}" "reject block3-bad-reward"
expect_field "${BODY}" error BAD_COINBASE_AMOUNT "coinbase error code"
split_resp "$(req POST /blocks examples/block3-bad-prev.json)"
expect_status 400 "${STATUS}" "reject block3-bad-prev"
expect_field "${BODY}" error PREV_HASH_MISMATCH "prevHash error code"

T="$(curl -sS "${BASE}/tip")"
expect_field "${T}" height 2 "tip unchanged at 2 after all rejections"
TX2="$(python3 -c "import json;print(json.load(open('examples/expected.json'))['block2.spendTxid'])")"
split_resp "$(req GET "/utxos/${TX2}/1?height=2")"
expect_status 200 "${STATUS}" "double-spent input restored to unspent"

echo "== Accept the valid block 3 =="
split_resp "$(req POST /blocks examples/block3.json)"
expect_status 201 "${STATUS}" "accept block3"
expect_known "${BODY}" state_root block3.stateRoot "block3"
expect_field "${BODY}" fees 1 "block3 collects 1 fee"

echo "== History requires explicit height and must not use current UTXO =="
split_resp "$(req GET "/state-root")"; expect_status 400 "${STATUS}" "state-root without height rejected"
split_resp "$(req GET "/utxos/${TX2}/1")"; expect_status 400 "${STATUS}" "utxo without height rejected"
split_resp "$(req GET "/utxos/${TX2}/1?height=2")"; expect_status 200 "${STATUS}" "change output unspent at height 2"
split_resp "$(req GET "/utxos/${TX2}/1?height=3")"; expect_status 404 "${STATUS}" "same output spent at height 3"
split_resp "$(req GET "/state-root?height=2")"
R2_HIST="${BODY}"
expect_known "${R2_HIST}" state_root block2.stateRoot "state root at height 2 stays stable"
split_resp "$(req GET "/state-root?height=99")"; expect_status 404 "${STATUS}" "unknown height 404"

echo "== Naive replay vs incremental implementation =="
split_resp "$(req GET /replay/verify)"
expect_status 200 "${STATUS}" "replay endpoint"
expect_field "${BODY}" all_match true "replay roots all match"
NHEIGHTS="$(printf '%s' "${BODY}" | jget "['heights']" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
[[ "${NHEIGHTS}" == "4" ]] && ok "replay covers genesis + 3 blocks" || bad "replay height count ${NHEIGHTS}"

echo "== Consecutive disconnect (two blocks in one atomic call), then reorg =="
split_resp "$(reqraw POST /blocks/disconnect '{"targetHeight":1}')"
expect_status 200 "${STATUS}" "disconnect to height 1"
expect_field "${BODY}" disconnected 2 "rolled back exactly two blocks"
expect_known "${BODY}" tip_hash afterDisconnect2.tipHash "tip hash back to block1"
expect_known "${BODY}" state_root afterDisconnect2.stateRoot "state root restored to block1"

split_resp "$(req POST /blocks examples/alt-block2.json)"
expect_status 201 "${STATUS}" "accept alternative block2"
expect_known "${BODY}" hash alt.block2.hash "alt block2 hash"
expect_field "${BODY}" fees 960 "alt block2 fee"

# Now that the alternative tip is height 2, an alt block 3 that spends an
# output only the ABANDONED branch created must fail with UNKNOWN_INPUT.
split_resp "$(req POST /blocks examples/alt-block3-stale-input.json)"
expect_status 400 "${STATUS}" "illegal alternative (spends abandoned-branch output) rejected"
expect_field "${BODY}" error UNKNOWN_INPUT "stale input error code"

split_resp "$(req POST /blocks examples/alt-block3.json)"
expect_status 201 "${STATUS}" "accept alternative block3"
expect_known "${BODY}" state_root alt.block3.stateRoot "alt block3 state root"

split_resp "$(req GET /replay/verify)"
expect_field "${BODY}" all_match true "replay matches the reorged chain"

echo "== Restart persistence =="
kill "${SERVER_PID}"; wait "${SERVER_PID}" 2>/dev/null || true
UTXO_DB="${DB}" UTXO_ADDR="${ADDR}" ./target/debug/utxo-rollback >"${LOG}" 2>&1 &
SERVER_PID=$!
READY=""
for _ in $(seq 1 50); do
  curl -sS "${BASE}/health" 2>/dev/null | grep -q '"status":"ok"' && { READY=1; break; }
  sleep 0.2
done
[[ -n "${READY}" ]] || { echo "server did not restart; log:"; cat "${LOG}"; exit 1; }
T="$(curl -sS "${BASE}/tip")"
expect_field "${T}" height 3 "tip height after restart"
expect_known "${T}" state_root alt.block3.stateRoot "state root after restart"
split_resp "$(req GET /replay/verify)"
expect_field "${BODY}" all_match true "replay after restart"
split_resp "$(req POST /blocks examples/block1.json)"
expect_status 400 "${STATUS}" "re-submitting old block rejected after restart"

echo
echo "================ SUMMARY ================"
echo "passed: ${PASS}, failed: ${FAIL}"
if [[ "${FAIL}" != "0" ]]; then
  echo "ACCEPTANCE FAILED"
  echo "server log: ${LOG} (kept until trap exit)"
  exit 1
fi
echo "ALL ACCEPTANCE CHECKS PASSED"
