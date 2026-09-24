#!/usr/bin/env bash
#
# End-to-end acceptance for the multi-robot namespace-isolation gateway.
#
# Boots REAL processes (nothing mocked):
#   - the FastAPI/rclpy gateway,
#   - two synthetic robots in distinct namespaces (alpha, beta),
# then exercises with curl:
#   1. registration + token issuance (real HMAC)
#   2. unicast: alpha command -> only alpha's ROS topic receives it
#   3. same relative topic name (cmd/move) in two namespaces stays isolated
#   4. expired command dropped with reason recorded
#   5. sequence rollback rejected
#   6. cross-robot token reuse rejected (越权)
#   7. absolute/escaping topic rejected
#   8. mapping remap immediately invalidates the old token
#   9. state query is scoped to one robot
#
# Exit non-zero if any check fails. ROS logs are kept under scripts/_run.
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# ---- environment ---------------------------------------------------------- #
if [[ -f /opt/ros/jazzy/setup.bash ]]; then
  # shellcheck disable=SC1091
  # Sourced before `set -u`: ROS setup scripts reference unbound vars.
  source /opt/ros/jazzy/setup.bash
else
  echo "ROS2 (jazzy) not found at /opt/ros/jazzy" >&2; exit 2
fi

set -u
export ROS_LOCALHOST_ONLY=1
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((30 + RANDOM % 60))}"
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"
export MRGW_HMAC_SECRET="acceptance-hmac-secret"
export MRGW_ADMIN_TOKEN="acceptance-admin-token"
export MRGW_PORT="${MRGW_PORT:-18000}"
export MRGW_AUDIT_LOG="$ROOT/scripts/_run/acceptance.jsonl"
BASE="http://127.0.0.1:${MRGW_PORT}"

RUN="$ROOT/scripts/_run"
mkdir -p "$RUN"
GW_LOG="$RUN/gateway.log"; RA_LOG="$RUN/robot_alpha.log"; RB_LOG="$RUN/robot_beta.log"
: > "$GW_LOG"; : > "$RA_LOG"; : > "$RB_LOG"; : > "$MRGW_AUDIT_LOG"

PASS=0; FAIL=0
ok()   { echo "PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $*"; FAIL=$((FAIL+1)); }
# check "<label>" <command...> — run the remaining args directly; pass on
# exit 0. No eval, so arguments may freely contain quotes or JSON.
check(){ local label="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
notgrep(){ ! grep -q "$1" "$2"; }
jsonhas(){ python3 - "$1" "$2" <<'PY'
import json, sys
needle, path = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
raise SystemExit(0 if needle in json.dumps(data) else 1)
PY
}

require(){ command -v "$1" >/dev/null 2>&1 || { echo "need $1" >&2; exit 2; }; }
require curl; require python3

cleanup(){
  [[ -n "${RB_PID:-}" ]]  && kill "$RB_PID"  2>/dev/null
  [[ -n "${RA_PID:-}" ]]  && kill "$RA_PID"  2>/dev/null
  [[ -n "${GW_PID:-}" ]]  && kill "$GW_PID"  2>/dev/null
}
trap cleanup EXIT

# ---- start processes ------------------------------------------------------ #
echo "== starting gateway on :${MRGW_PORT} (domain ${ROS_DOMAIN_ID})"
python3 -m mr_gateway.main > "$GW_LOG" 2>&1 &
GW_PID=$!

for i in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done
check "gateway health" curl -sf "$BASE/health"

python3 -m mr_gateway.synthetic_robot alpha --namespace team/alpha \
  --topics cmd/move,cmd/stop > "$RA_LOG" 2>&1 &
RA_PID=$!
python3 -m mr_gateway.synthetic_robot beta --namespace team/beta \
  --topics cmd/move > "$RB_LOG" 2>&1 &
RB_PID=$!

# ---- 1. registration ------------------------------------------------------ #
reg_a=$(curl -s -X POST -H "X-Admin-Token: ${MRGW_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d @"$ROOT/examples/register_alpha.json" "$BASE/admin/robots/alpha")
echo "$reg_a" > "$RUN/reg_alpha.json"
check "register alpha (epoch 1)" jsonhas '"epoch": 1' "$RUN/reg_alpha.json"
reg_b=$(curl -s -X POST -H "X-Admin-Token: ${MRGW_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d @"$ROOT/examples/register_beta.json" "$BASE/admin/robots/beta")
echo "$reg_b" > "$RUN/reg_beta.json"
check "register beta (epoch 1)" jsonhas '"epoch": 1' "$RUN/reg_beta.json"

# wait for DDS to wire subscribers (prewarmed publishers must exist)
echo "== waiting for DDS endpoint matching"
for i in $(seq 1 50); do
  n=$(curl -sf "$BASE/health" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['ros']['publishers']))" 2>/dev/null || echo 0)
  [[ "$n" -ge 2 ]] && break
  sleep 0.2
done
sleep 2  # let the latched endpoints fully match

# ---- 2. tokens ------------------------------------------------------------ #
TA=$(curl -sf -X POST -H "X-Admin-Token: ${MRGW_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d @"$ROOT/examples/token_request.json" "$BASE/admin/robots/alpha/tokens" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
TB=$(curl -sf -X POST -H "X-Admin-Token: ${MRGW_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"tester_id":"tester-bob"}' "$BASE/admin/robots/beta/tokens" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
[[ -n "$TA" ]] && ok "issue alpha token" || bad "issue alpha token"
[[ -n "$TB" ]] && ok "issue beta token"  || bad "issue beta token"

send(){ # robot token seq payload [target] [ttl]
  local robot="$1" tok="$2" seq="$3" payload="$4" target="${5:-cmd/move}" ttl="${6:-30}"
  curl -s -o "$RUN/resp_${robot}_${seq}.json" -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
    -d "{\"target\":\"$target\",\"sequence\":$seq,\"ttl_seconds\":$ttl,\"tester_id\":\"tester-alice\",\"payload\":$payload}" \
    "$BASE/robots/$robot/commands"
}

# ---- 3. unicast alpha ----------------------------------------------------- #
code=$(send alpha "$TA" 1 '{"action":"goto","x":1.0}')
[[ "$code" == "200" ]] && ok "alpha cmd seq1 accepted (200)" \
  || bad "alpha cmd seq1 accepted (got $code)"
check "published topic is /team/alpha/cmd/move" \
  jsonhas '"topic": "/team/alpha/cmd/move"' "$RUN/resp_alpha_1.json"
sleep 1.5
check "alpha robot received seq1" grep -q 'seq=1' "$RA_LOG"
check "beta robot did NOT receive alpha seq1" notgrep 'seq=1' "$RB_LOG"

# ---- 4. same-name topic isolation: beta cmd/move -------------------------- #
curl -s -o /dev/null -X POST -H "Authorization: Bearer $TB" \
  -H 'Content-Type: application/json' \
  -d '{"target":"cmd/move","sequence":1,"ttl_seconds":30,"tester_id":"tester-bob","payload":{"who":"beta"}}' \
  "$BASE/robots/beta/commands"
sleep 1.5
check "beta robot received its own seq1" grep -q 'from_ns=team/beta' "$RB_LOG"
check "alpha robot never saw beta namespace" notgrep 'team/beta' "$RA_LOG"

# ---- 5. expired command --------------------------------------------------- #
past=$(python3 -c 'import time;print(time.time()-10)')
code=$(curl -s -o "$RUN/expired.json" -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
  -d "{\"target\":\"cmd/move\",\"sequence\":9,\"expires_at\":$past,\"tester_id\":\"tester-alice\",\"payload\":{}}" \
  "$BASE/robots/alpha/commands")
[[ "$code" == "400" ]] && ok "expired command rejected (400)" \
  || bad "expired command rejected (got $code)"
check "expired reason recorded in body" grep -q 'command_expired' "$RUN/expired.json"
check "expired reason recorded in audit log" grep -q 'command_expired' "$MRGW_AUDIT_LOG"

# ---- 6. sequence rollback ------------------------------------------------- #
send alpha "$TA" 5 '{"n":5}' >/dev/null
code=$(send alpha "$TA" 4 '{"n":4}')
[[ "$code" == "409" ]] && ok "rollback seq 5->4 rejected (409)" \
  || bad "rollback seq 5->4 rejected (got $code)"
check "rollback reason = sequence_rollback" \
  grep -q sequence_rollback "$RUN/resp_alpha_4.json"

# ---- 7. cross-robot token (越权) ------------------------------------------ #
code=$(curl -s -o "$RUN/cross.json" -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
  -d '{"target":"cmd/move","sequence":1,"ttl_seconds":30,"tester_id":"tester-alice","payload":{}}' \
  "$BASE/robots/beta/commands")
[[ "$code" == "401" ]] && ok "alpha token cannot command beta (401)" \
  || bad "alpha token cannot command beta (got $code)"
check "cross-robot reason = token_signature_invalid" \
  grep -q token_signature_invalid "$RUN/cross.json"

# ---- 8. topic escape ------------------------------------------------------ #
code=$(curl -s -o "$RUN/escape.json" -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
  -d '{"target":"/team/beta/cmd/move","sequence":6,"ttl_seconds":30,"tester_id":"tester-alice","payload":{}}' \
  "$BASE/robots/alpha/commands")
[[ "$code" == "400" ]] && ok "absolute topic rejected (400)" \
  || bad "absolute topic rejected (got $code)"

# ---- 9. remap invalidates old token immediately --------------------------- #
remap_resp=$(curl -s -X POST -H "X-Admin-Token: ${MRGW_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"namespace":"team/alpha_v2","prewarm_topics":["cmd/move"]}' \
  "$BASE/admin/robots/alpha")
echo "$remap_resp" > "$RUN/remap.json"
check "remap bumps alpha epoch to 2" jsonhas '"epoch": 2' "$RUN/remap.json"
code=$(send alpha "$TA" 7 '{"after":"remap"}')
[[ "$code" == "401" ]] && ok "old token rejected after remap (401)" \
  || bad "old token rejected after remap (got $code)"
check "post-remap reason = stale_authorization" \
  grep -q stale_authorization "$RUN/resp_alpha_7.json"

# ---- 10. query isolation -------------------------------------------------- #
code=$(curl -s -o "$RUN/state.json" -w '%{http_code}' \
  -H "Authorization: Bearer $TB" "$BASE/robots/beta/state")
[[ "$code" == "200" ]] && ok "beta state query 200" \
  || bad "beta state query (got $code)"
check "beta state leaks no alpha namespace" notgrep 'alpha' "$RUN/state.json"

# ---- summary -------------------------------------------------------------- #
echo
echo "================= ACCEPTANCE SUMMARY ================="
echo "PASS=$PASS FAIL=$FAIL"
echo "gateway log: $GW_LOG ; audit: $MRGW_AUDIT_LOG"
[[ "$FAIL" -eq 0 ]]
