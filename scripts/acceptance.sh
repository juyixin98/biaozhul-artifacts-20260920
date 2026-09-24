#!/usr/bin/env bash
# End-to-end acceptance test for the Task Resource Deadlock Check service,
# driven entirely through the real HTTP API.
#
# Demonstrates and verifies:
#   1. atomic all-or-nothing acquisition (no partial holds)
#   2. cross-resource requests with explicit wait reasons + signed evidence
#   3. dynamic wait-for graph cycle rejection
#   4. timeout -> uncertain fencing; resources are NOT handed to the next task
#   5. late completion after timeout
#   6. priority aging promotion order
#   7. run-time revoke releases only after confirmed stop
#
# The server should be started with fast aging so this stays short:
#   AGING_STEP_MS=100 SWEEP_INTERVAL=500ms ./bin/deadlock-server
# (scripts/run-demo.sh starts it this way.)
#
# Usage: scripts/acceptance.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8068}"
AGING_MS="${AGING_STEP_MS:-100}"   # must match the server's AGING_STEP_MS
PASS=0; FAIL=0
ok()  { printf '  \033[32m[ok]\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad() { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
say() { printf '\n\033[1;34m== %s ==\033[0m\n' "$1"; }

jqr() { jq -r "$2" "$1" 2>/dev/null; }

api() { # method path [json-file]
  local m="$1" p="$2" f="${3:-}"
  if [[ -n "$f" ]]; then
    curl -sS -X "$m" -H 'Content-Type: application/json' --data-binary "@$f" "$BASE$p"
  else
    curl -sS -X "$m" "$BASE$p"
  fi
}

complete() { # taskID epoch  -> complete with a fence token
  curl -sS -X POST -H 'Content-Type: application/json' \
    -d "{\"fenceEpoch\":$2}" "$BASE/v1/tasks/$1/complete"
}

assert_eq() { # actual expected label
  if [[ "$1" == "$2" ]]; then ok "$3 (= $1)"; else bad "$3: got '$1' want '$2'"; fi
}

# Make the script idempotent: reset business data (keep meta: signing key,
# server id and fence epoch survive, like a normal production restart).
DB_RESET="${DB_RESET:-1}"
if [[ "$DB_RESET" == "1" ]]; then
  PGPASSWORD="${DB_PASSWORD:-deadlock_pw_068}" psql -h localhost -U "${DB_ROLE:-deadlock}" \
    -d "${DB_NAME:-deadlock_db}" -q 2>/dev/null <<'SQL' \
    || echo "    (warning: DB reset skipped; psql unavailable)"
TRUNCATE task_events, hold_ledger, task_resources, tasks, resources RESTART IDENTITY CASCADE;
SQL
true
fi

say "health"
assert_eq "$(api GET /healthz | jq -r .status)" "ok" "service is up"

# ------------------------------------------------------------- 1. resources
say "registering tools and workstations"
for f in examples/resource-*.json; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
        --data-binary "@$f" "$BASE/v1/resources")
  [[ "$code" == "201" ]] && ok "registered $(basename "$f")" || bad "register $f -> $code"
done

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ----------------------------------------- 2. atomic + cross-resource waits
say "A atomically acquires arm+welder+bayA"
api POST /v1/tasks examples/task-A.json > "$TMP/A.json"
assert_eq "$(jqr "$TMP/A.json" .status)" "granted" "A granted the whole group"
assert_eq "$(jqr "$TMP/A.json" '.holds|length')" "3" "A holds exactly 3 resources"
EPOCH_A=$(jqr "$TMP/A.json" .task.fenceEpoch)

say "B crosses over the arm (held by A) -> must WAIT and hold nothing"
api POST /v1/tasks examples/task-B.json > "$TMP/B.json"
EPOCH_B=$(jqr "$TMP/B.json" .task.fenceEpoch)
assert_eq "$(jqr "$TMP/B.json" .status)" "waiting" "B is waiting"
assert_eq "$(jqr "$TMP/B.json" '(.holds // [])|length')" "0" "B holds ZERO resources (atomic, no partial)"
HOLDER=$(jqr "$TMP/B.json" '.waitReasons[]|select(.resourceId=="t-arm-1").blockedByTask')
assert_eq "$HOLDER" "task-weld-A" "wait reason names task-weld-A as arm holder"

say "holding evidence: Ed25519 token verifies; tampering is detected"
TOKEN=$(jqr "$TMP/A.json" '.holds[0].evidenceToken')
echo "{\"token\":\"$TOKEN\"}" > "$TMP/tok.json"
VPAY=$(api POST /v1/verify-token "$TMP/tok.json")
assert_eq "$(echo "$VPAY" | jq -r .taskId)" "task-weld-A" "valid signature, payload intact"
echo "{\"token\":\"${TOKEN%??}AA\"}" > "$TMP/tokbad.json"
VCODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
        --data-binary "@$TMP/tokbad.json" "$BASE/v1/verify-token")
assert_eq "$VCODE" "409" "tampered evidence token rejected"

# --------------------------------------------- 3. dynamic dependency cycles
say "dynamic graph: requests that would close a cycle are rejected"
echo '{"id":"s-temp","kind":"station","description":"temp lane"}' > "$TMP/temp.json"
api POST /v1/resources "$TMP/temp.json" >/dev/null
# Y holds s-temp, then wants the arm held by A -> edge Y -> A
echo '{"id":"cyc-Y","priority":100,"resources":["s-temp"],"deadlineMs":60000}' > "$TMP/Y.json"
api POST /v1/tasks "$TMP/Y.json" >/dev/null
echo '{"resources":["t-arm-1"]}' > "$TMP/addarm.json"
assert_eq "$(api POST /v1/tasks/cyc-Y/resources "$TMP/addarm.json" | jq -r .status)" "waiting" \
  "Y queued behind A (edge Y -> A)"
# A now asks for s-temp held by Y -> closes A -> Y -> A
echo '{"resources":["s-temp"]}' > "$TMP/addtemp.json"
CYC=$(api POST /v1/tasks/task-weld-A/resources "$TMP/addtemp.json")
assert_eq "$(echo "$CYC" | jq -r .status)" "rejected" "cycle-forming dynamic request REJECTED"
[[ "$(echo "$CYC" | jq -r '.cycles|length')" -ge 1 ]] \
  && ok "rejection carries the cycle path: $(echo "$CYC" | jq -c '.cycles[0].tasks')" \
  || bad "no cycle reported"
assert_eq "$(api GET /v1/tasks/task-weld-A | jq -r .task.state)" "running" "A keeps running with original holds"
api GET /v1/graph | jq -e '.edges|length >= 1' >/dev/null \
  && ok "graph view exposes live wait-for edges" || bad "graph edges missing"

# A completes (with its fence epoch) -> its resources release. B was promoted
# once A freed the arm, so complete B as well to leave bayB free for the
# timeout demonstration.
complete task-weld-A "$EPOCH_A" >/dev/null
sleep 0.2
assert_eq "$(api GET /v1/tasks/task-assy-B | jq -r .task.state)" "running" \
  "B promoted atomically after A released the arm"
complete task-assy-B "$EPOCH_B" >/dev/null

# --------------------------------------- 4. timeout fencing + late complete
say "timeout: expired holder becomes UNCERTAIN and keeps its resource fenced"
echo '{"id":"tt-holder","priority":100,"resources":["s-bay-b"],"deadlineMs":400}' > "$TMP/h.json"
api POST /v1/tasks "$TMP/h.json" > "$TMP/hresp.json"
H_EPOCH=$(jqr "$TMP/hresp.json" .task.fenceEpoch)
echo '{"id":"tt-next","priority":100,"resources":["s-bay-b"],"deadlineMs":60000}' > "$TMP/n.json"
api POST /v1/tasks "$TMP/n.json" >/dev/null
echo "    (waiting 1.2s for the 400ms lease to expire...)"
sleep 1.2
# The server's background sweeper may have moved it already; the explicit
# sweep call is idempotent. Accept either path, the asserted end state is what matters.
api POST /v1/sweep-timeouts > "$TMP/sweep.json"
SWEEPED=$(jqr "$TMP/sweep.json" '.timedOut|index("tt-holder")!=null')
ALREADY=$(api GET /v1/tasks/tt-holder | jq -r .task.state)
{ [[ "$SWEEPED" == "true" ]] || [[ "$ALREADY" == "uncertain" ]]; } \
  && ok "holder reached uncertain (via sweeper or explicit sweep)" \
  || bad "holder not timed out: swept=$SWEEPED state=$ALREADY"
assert_eq "$(api GET /v1/tasks/tt-holder | jq -r .task.state)" "uncertain" "holder state=uncertain"
assert_eq "$(api GET /v1/tasks/tt-next | jq -r .task.state)" "waiting" \
  "next task does NOT receive the fenced resource"
assert_eq "$(api GET /v1/tasks/tt-next/waits | jq -r '.waitReasons[0].holderState')" "uncertain" \
  "wait reason explicitly shows the uncertain fence"
HBCODE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
        -d "{\"fenceEpoch\":$H_EPOCH}" "$BASE/v1/tasks/tt-holder/heartbeat")
assert_eq "$HBCODE" "409" "heartbeat refused once fenced"

say "late completion with original epoch is honoured -> next task promoted"
LC=$(complete tt-holder "$H_EPOCH")
assert_eq "$(echo "$LC" | jq -r .task.state)" "completed" "late completion accepted"
assert_eq "$(api GET /v1/tasks/tt-next | jq -r .task.state)" "running" "next task now runs"

# ------------------------------------------------ 5. priority aging
# OLD has base priority 100, NEW has 50 (more urgent). OLD enqueues ~55 aging
# steps earlier, so by release time OLD's effective priority beats NEW's.
say "priority aging overtakes a newer higher-priority waiter"
STEPS=55
HEADSTART=$(awk "BEGIN{printf \"%.1f\", $STEPS*$AGING_MS/1000.0 + 0.3}")
echo '{"id":"s-age","kind":"station","description":"aging lane"}' > "$TMP/age.json"
api POST /v1/resources "$TMP/age.json" >/dev/null
echo '{"id":"age-H","priority":1,"resources":["s-age"],"deadlineMs":60000}'    > "$TMP/ageH.json"
echo '{"id":"age-OLD","priority":100,"resources":["s-age"],"deadlineMs":60000}' > "$TMP/ageO.json"
echo '{"id":"age-NEW","priority":50,"resources":["s-age"],"deadlineMs":60000}'  > "$TMP/ageN.json"
api POST /v1/tasks "$TMP/ageH.json" > "$TMP/ageHresp.json"
AGEH_EPOCH=$(jqr "$TMP/ageHresp.json" .task.fenceEpoch)
api POST /v1/tasks "$TMP/ageO.json" >/dev/null
echo "    OLD (prio 100) ages for ${HEADSTART}s before NEW (prio 50) enqueues..."
sleep "$HEADSTART"
api POST /v1/tasks "$TMP/ageN.json" >/dev/null
sleep 0.5
EP_OLD=$(api GET /v1/tasks | jq -r '.tasks[]|select(.id=="age-OLD").effectivePriority')
EP_NEW=$(api GET /v1/tasks | jq -r '.tasks[]|select(.id=="age-NEW").effectivePriority')
echo "    effective priorities before release: OLD=$EP_OLD NEW=$EP_NEW"
[[ "$EP_OLD" -lt "$EP_NEW" ]] && ok "OLD has aged past NEW in effective priority" \
  || bad "aging insufficient: OLD=$EP_OLD NEW=$EP_NEW (need AGING_STEP_MS=$AGING_MS)"
complete age-H "$AGEH_EPOCH" >/dev/null
assert_eq "$(api GET /v1/tasks/age-OLD | jq -r .task.state)" "running" \
  "aged low-priority OLD promoted first"
assert_eq "$(api GET /v1/tasks/age-NEW | jq -r .task.state)" "waiting" \
  "newer high-priority NEW still waits"

# --------------------------------------------- 6. revoke handshake
say "run-time revoke releases resources only after stop is confirmed"
echo '{"id":"rv-res","kind":"tool","description":"revoke lane"}' > "$TMP/rvr.json"
api POST /v1/resources "$TMP/rvr.json" >/dev/null
echo '{"id":"rv-H","priority":100,"resources":["rv-res"],"deadlineMs":60000}' > "$TMP/rvH.json"
echo '{"id":"rv-W","priority":100,"resources":["rv-res"],"deadlineMs":60000}' > "$TMP/rvW.json"
api POST /v1/tasks "$TMP/rvH.json" > "$TMP/rvHresp.json"
RVH_EPOCH=$(jqr "$TMP/rvHresp.json" .task.fenceEpoch)
api POST /v1/tasks "$TMP/rvW.json" >/dev/null
assert_eq "$(api POST /v1/tasks/rv-H/revoke | jq -r .task.state)" "revoking" "holder enters revoking"
assert_eq "$(api GET /v1/tasks/rv-W | jq -r .task.state)" "waiting" \
  "waiter still blocked: revoke alone frees nothing"
# A stale worker that ignores the revoke still cannot complete with its epoch:
STALE=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
       -d "{\"fenceEpoch\":$RVH_EPOCH}" "$BASE/v1/tasks/rv-H/complete")
assert_eq "$STALE" "409" "completion refused while revocation is pending"
api POST /v1/tasks/rv-H/confirm-stop >/dev/null
assert_eq "$(api GET /v1/tasks/rv-W | jq -r .task.state)" "running" \
  "waiter runs only after confirmed stop"

# ------------------------------------------------------- 7. audit evidence
say "audit trail: grant/wait/timeout/completion events persisted"
NEV=$(api GET /v1/tasks/tt-holder/events | jq '.events|length')
[[ "$NEV" -ge 3 ]] && ok "tt-holder ledger has $NEV events (acquire, timeout, complete)" \
  || bad "event ledger sparse: $NEV"
LED=$(PGPASSWORD=deadlock_pw_068 psql -h localhost -U deadlock -d deadlock_db -tAc \
  "SELECT string_agg(action,',' ORDER BY id) FROM hold_ledger WHERE task_id='tt-holder'")
assert_eq "$LED" "wanted,granted,released" "hold ledger chain wanted->granted->released"

say "restart recovery (covered automatically in Go: TestRestartRecovery)"
echo "    restart the server and watch startup log:"
echo "      recovery: epoch=N running->uncertain=[...] waiting=[...]"
echo "    a stale complete/heartbeat carrying the old epoch then returns HTTP 409."

printf '\n\033[1;33mRESULT: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
