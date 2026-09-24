#!/usr/bin/env bash
# End-to-end demo of the gang scheduler's atomic guarantees.
#
# Scenario:
#   nodes n1(role=a) n2(role=shared) n3(role=b)
#   gA needs n1+n2 ; gB needs n2+n3  -> they fight over n2
#   1. gA reserves, gB waits holding nothing
#   2. n2 goes offline between reserve and commit -> gA commit 410, zero leaks
#   3. recovery: n2 back, gA commits+completes -> gB atomically takes n2+n3
#   4. stale optimistic version -> 409, nothing starts
#   5. short-TTL reservation expires in the background -> 410 on commit
#
# Requires: bash, curl, python3 (pretty JSON). No build step beyond `go build`.
set -u
cd "$(dirname "$0")/.."

# ADDR is optional; when unset the script picks a free loopback port.
ADDR="${ADDR:-}"
PASS=0; FAIL=0

# req METHOD PATH [JSON_BODY] -> pretty-prints body, returns status via $STATUS,
# keeps the RAW JSON in $BODY for extract().
req() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" -H 'Content-Type: application/json' -w '\n%{http_code}')
  if [ -n "$body" ]; then args+=(--data "$body"); fi
  local out
  out="$(curl "${args[@]}" "$BASE$path")"
  STATUS="${out##*$'\n'}"
  BODY="${out%$'\n'*}"
  echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
}

expect() { # desc expected_status actual_status
  if [ "$2" = "$3" ]; then
    echo "  [OK] $1 ($3)"; PASS=$((PASS+1))
  else
    echo "  [FAIL] $1: want $2 got $3"; FAIL=$((FAIL+1))
  fi
}

extract() { # FIELD  (extracts scalar from last $BODY via python)
  python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print($1)" <<<"$BODY"
}

section() { echo; echo "=== $* ==="; }

trap 'kill "$SRV_PID" 2>/dev/null' EXIT

section "build & start server (ttl=2s for the expiry demo)"
export PATH="$PATH:/usr/local/go/bin"
go build -o /tmp/gangsrv . || exit 1

# Pick a free loopback port unless the caller fixed ADDR.
if [ -z "${ADDR:-}" ]; then
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
  ADDR="127.0.0.1:$PORT"
fi
BASE="http://$ADDR"
echo "  using $BASE"
/tmp/gangsrv -addr="$ADDR" -ttl=2s -sweep=100ms >/tmp/gangsrv.log 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do
  kill -0 "$SRV_PID" 2>/dev/null || { echo "server exited early:"; cat /tmp/gangsrv.log; exit 1; }
  # Confirm it is OUR server: /state must expose the gang scheduler fields.
  curl -sS "$BASE/state" 2>/dev/null | grep -q waiting_queue && break
  sleep 0.1
done
curl -sS "$BASE/state" | grep -q waiting_queue || { echo "server not ready:"; cat /tmp/gangsrv.log; exit 1; }

section "0. health"
req GET /healthz
expect "healthz 200" 200 "$STATUS"

section "1. register three nodes"
req POST /nodes '{"id":"n1","zone":"z1","capacity":1,"labels":{"role":"a"}}'
expect "add n1 -> 201" 201 "$STATUS"
req POST /nodes '{"id":"n2","zone":"z1","capacity":1,"labels":{"role":"shared"}}'
expect "add n2 -> 201" 201 "$STATUS"
req POST /nodes '{"id":"n3","zone":"z2","capacity":1,"labels":{"role":"b"}}'
expect "add n3 -> 201" 201 "$STATUS"

section "2. gA (needs n1+n2) reserves atomically"
req POST /gangs '{"id":"gA","min_nodes":2,"ttl_millis":60000,"tasks":[{"name":"a1","node_selector":{"role":"a"}},{"name":"a2","node_selector":{"role":"shared"}}]}'
expect "gA -> 201" 201 "$STATUS"
GA_PLAN="$(extract 'd["plan"]["id"]')"
GA_VER="$(extract 'd["plan"]["version"]')"
echo "  gA plan=$GA_PLAN version=$GA_VER"

section "3. gB (needs n2+n3) overlaps gA -> must WAIT and hold nothing"
req POST /gangs '{"id":"gB","min_nodes":2,"ttl_millis":60000,"tasks":[{"name":"b1","node_selector":{"role":"shared"}},{"name":"b2","node_selector":{"role":"b"}}]}'
expect "gB -> 201" 201 "$STATUS"
GB_STATUS="$(extract 'd["gang"]["status"]')"
[ "$GB_STATUS" = "waiting" ] && { echo "  [OK] gB is waiting ($GB_STATUS)"; PASS=$((PASS+1)); } || { echo "  [FAIL] gB=$GB_STATUS"; FAIL=$((FAIL+1)); }
req GET /nodes/n3
N3_HELD="$(extract 'd["held_slots"]')"
[ "$N3_HELD" = "0" ] && { echo "  [OK] gB holds zero slots even on free n3"; PASS=$((PASS+1)); } || { echo "  [FAIL] n3 held=$N3_HELD"; FAIL=$((FAIL+1)); }

section "4. n2 goes OFFLINE between reserve and commit"
req POST /nodes/n2/state '{"online":false}'
expect "offline held n2 -> 200 (plan invalidated)" 200 "$STATUS"

section "5. commit gA -> must be 410 Gone; NO partial start, NO leak"
req POST "/gangs/gA/plans/$GA_PLAN/commit" "{\"version\":$GA_VER}"
expect "commit after offline -> 410" 410 "$STATUS"
req GET /state
LEAK="$(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(sum(n["held_slots"]+n["running_slots"] for n in d["nodes"]))' <<<"$BODY")"
[ "$LEAK" = "0" ] && { echo "  [OK] total held/running slots after abort = 0 (no leak)"; PASS=$((PASS+1)); } || { echo "  [FAIL] leaked slots=$LEAK"; FAIL=$((FAIL+1)); }

section "6. recovery: n2 back online -> gA reserves again, commits, completes"
req POST /nodes/n2/state '{"online":true}'
expect "n2 online -> 200" 200 "$STATUS"
req GET /gangs/gA
GA_PLAN2="$(extract 'd["plan"]["id"]')"
GA_VER2="$(extract 'd["plan"]["version"]')"
req POST "/gangs/gA/plans/$GA_PLAN2/commit" "{\"version\":$GA_VER2}"
expect "gA commit -> 200" 200 "$STATUS"
req POST /nodes/n2/state '{"online":false}'
expect "offline RUNNING node -> 409" 409 "$STATUS"
req POST /gangs/gA/complete '{}'
expect "gA complete -> 200" 200 "$STATUS"

section "7. gB now atomically reserves n2+n3 and commits"
req GET /gangs/gB
GB_STATUS="$(extract 'd["gang"]["status"]')"
[ "$GB_STATUS" = "reserved" ] && { echo "  [OK] gB auto-reserved ($GB_STATUS)"; PASS=$((PASS+1)); } || { echo "  [FAIL] gB=$GB_STATUS"; FAIL=$((FAIL+1)); }
GB_PLAN="$(extract 'd["plan"]["id"]')"
GB_VER="$(extract 'd["plan"]["version"]')"
GB_NODES="$(extract '",".join(d["plan"]["nodes"])')"
echo "  gB nodes=$GB_NODES"
[ "$GB_NODES" = "n2,n3" ] && { echo "  [OK] gB got exactly n2,n3 (no double booking)"; PASS=$((PASS+1)); } || { echo "  [FAIL] nodes=$GB_NODES"; FAIL=$((FAIL+1)); }
req POST "/gangs/gB/plans/$GB_PLAN/commit" "{\"version\":$GB_VER}"
expect "gB commit -> 200" 200 "$STATUS"
req POST /gangs/gB/complete '{}'
expect "gB complete -> 200" 200 "$STATUS"

section "8. anti-affinity: two ssd tasks across zones (n1 z1 + n3 z2)"
req POST /gangs '{"id":"gS","min_nodes":2,"ttl_millis":60000,"anti_affinity":"zone","tasks":[{"name":"s1","node_selector":{"role":"a"}},{"name":"s2","node_selector":{"role":"b"}}]}'
expect "gS -> 201" 201 "$STATUS"
GS_NODES="$(extract '",".join(d["plan"]["nodes"])')"
[ "$GS_NODES" = "n1,n3" ] && { echo "  [OK] spread across zones: $GS_NODES"; PASS=$((PASS+1)); } || { echo "  [FAIL] nodes=$GS_NODES"; FAIL=$((FAIL+1)); }
req POST "/gangs/gS/plans/$(extract 'd["plan"]["id"]')/release" '{}'
expect "release gS -> 200" 200 "$STATUS"

section "9. stale optimistic version -> 409, plan aborted, nothing starts"
req POST /gangs '{"id":"gV","min_nodes":1,"ttl_millis":60000,"tasks":[{"name":"v1"}]}'
expect "gV -> 201" 201 "$STATUS"
GV_PLAN="$(extract 'd["plan"]["id"]')"
GV_VER="$(extract 'd["plan"]["version"]')"
req POST "/gangs/gV/plans/$GV_PLAN/commit" "{\"version\":$((GV_VER-1))}"
expect "stale version commit -> 409" 409 "$STATUS"
req POST "/gangs/gV/plans/$GV_PLAN/commit" "{\"version\":$GV_VER}"
expect "re-commit aborted plan -> 410" 410 "$STATUS"

section "10. TTL expiry (server default ttl=2s): reserve, wait, commit -> 410"
# The abort above requeued gV and it auto-reserved a fresh plan; release it
# explicitly so the TTL gang has a free node (released gangs do NOT auto-reserve).
req GET /gangs/gV
GV_PLAN2="$(extract 'd["plan"]["id"] if d["plan"] else ""')"
if [ -n "$GV_PLAN2" ]; then
  req POST "/gangs/gV/plans/$GV_PLAN2/release" '{}'
  expect "release gV's fresh plan -> 200" 200 "$STATUS"
fi
req POST /gangs '{"id":"gT","min_nodes":1,"tasks":[{"name":"t1"}]}'
expect "gT -> 201" 201 "$STATUS"
GT_STATUS="$(extract 'd["gang"]["status"]')"
[ "$GT_STATUS" = "reserved" ] && { echo "  [OK] gT reserved"; PASS=$((PASS+1)); } || { echo "  [FAIL] gT=$GT_STATUS"; FAIL=$((FAIL+1)); }
GT_PLAN="$(extract 'd["plan"]["id"]')"
GT_VER="$(extract 'd["plan"]["version"]')"
echo "  sleeping 2.5s for the background reaper..."
sleep 2.5
req GET /gangs/gT
GT_STATUS="$(extract 'd["gang"]["status"]')"
[ "$GT_STATUS" = "expired" ] && { echo "  [OK] gT expired in background"; PASS=$((PASS+1)); } || { echo "  [FAIL] gT=$GT_STATUS"; FAIL=$((FAIL+1)); }
req POST "/gangs/gT/plans/$GT_PLAN/commit" "{\"version\":$GT_VER}"
expect "commit expired reservation -> 410" 410 "$STATUS"

section "11. final cluster state"
req GET /state

section "RESULT: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  echo "server log:"; cat /tmp/gangsrv.log
  exit 1
fi
