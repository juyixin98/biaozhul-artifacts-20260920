#!/usr/bin/env bash
# acceptance.sh — end-to-end acceptance for the test-result merging service.
#
# It really builds and runs the Go server and drives it over HTTP using only
# the fixture commands shipped under examples/fixtures (no cloud, no network).
# Exit code 0 means every acceptance assertion passed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Pick an ephemeral free port so a stale/unrelated process can't collide.
pick_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}
ADDR="127.0.0.1:$(pick_port)"
BASE="http://${ADDR}"
WORK="$(mktemp -d)"
CACHE="${WORK}/cache"
WDIR="${WORK}/work"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true' EXIT

pass=0; fail=0
jqr() { jq -r "$1" <<<"$2"; }
assert_eq() { # assert_eq <name> <expected> <actual>
  local name="$1" want="$2" got="$3"
  if [ "${want}" = "${got}" ]; then echo "  PASS  ${name}"; pass=$((pass+1));
  else echo "  FAIL  ${name} (want [${want}] got [${got}])"; fail=$((fail+1)); fi
}
assert_true() { # assert_true <name> <command...>
  local name="$1"; shift
  if "$@"; then echo "  PASS  ${name}"; pass=$((pass+1));
  else echo "  FAIL  ${name}"; fail=$((fail+1)); fi
}

wait_health() { for _ in $(seq 1 50); do curl -sf "${BASE}/healthz" >/dev/null && return 0; sleep 0.1; done; return 1; }
wait_finalized() { local id="$1" s; for _ in $(seq 1 80); do
  s="$(curl -sf "${BASE}/v1/runs/${id}/summary")"; [ "$(jqr '.finalized' "$s")" = "true" ] && { echo "$s"; return 0; }; sleep 0.25
done; echo "$s"; }

echo "== build =="
go build -o "${WORK}/trmerge" ./cmd/trmerge
echo "built ${WORK}/trmerge"

echo "== start server (cache and work dirs separate) =="
"${WORK}/trmerge" -addr "${ADDR}" -cache "${CACHE}" -work "${WDIR}" >"${WORK}/server.log" 2>&1 &
SRV_PID=$!
wait_health
echo "server up; cache=${CACHE} work=${WDIR}"
[ "${CACHE}" != "${WDIR}" ] && { echo "  PASS  cache dir differs from work dir"; pass=$((pass+1)); } || { echo "  FAIL  cache dir differs"; fail=$((fail+1)); }

echo
echo "== scenario A: events API with crash + retry + missing + cancel =="
RA="$(curl -sf -X POST "${BASE}/v1/runs" -H 'Content-Type: application/json' -d '{
  "mode":"events",
  "shards":[
    {"shard_id":"s1","test_ids":["auth.login","auth.logout","auth.expired_token"]},
    {"shard_id":"s2","test_ids":["billing.charge","billing.refund"]},
    {"shard_id":"s3","test_ids":["reports.export"]}
  ]}' | jq -r '.run_id')"
echo "run A = ${RA}"

tstat() { jqr '.tests[]|select(.test_id=="'"$2"'").status' "$1"; }

# s2 first (out of order): charge #1 fails, #2 starts with no result yet
curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' -d @- >/dev/null <<JSON
{"events":[
  {"event_id":"s2-start","type":"shard_started","shard_id":"s2"},
  {"event_id":"charge-a1-start","type":"attempt_started","shard_id":"s2","test_id":"billing.charge","attempt_id":"billing.charge#1","attempt_no":1},
  {"event_id":"charge-a1-fail","type":"attempt_result","shard_id":"s2","test_id":"billing.charge","attempt_id":"billing.charge#1","attempt_no":1,"result":"failed"},
  {"event_id":"charge-a2-start","type":"attempt_started","shard_id":"s2","test_id":"billing.charge","attempt_id":"billing.charge#2","attempt_no":2}
]}
JSON

# s1 events arrive later; refund on s2 too
curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' -d @- >/dev/null <<JSON
{"events":[
  {"event_id":"s1-start","type":"shard_started","shard_id":"s1"},
  {"event_id":"login-a1-start","type":"attempt_started","shard_id":"s1","test_id":"auth.login","attempt_id":"auth.login#1","attempt_no":1},
  {"event_id":"login-a1-pass","type":"attempt_result","shard_id":"s1","test_id":"auth.login","attempt_id":"auth.login#1","attempt_no":1,"result":"passed"},
  {"event_id":"logout-a1-start","type":"attempt_started","shard_id":"s1","test_id":"auth.logout","attempt_id":"auth.logout#1","attempt_no":1},
  {"event_id":"logout-a1-pass","type":"attempt_result","shard_id":"s1","test_id":"auth.logout","attempt_id":"auth.logout#1","attempt_no":1,"result":"passed"},
  {"event_id":"s1-fin","type":"shard_finished","shard_id":"s1","outcome":"completed","exit_code":0},
  {"event_id":"refund-a1-start","type":"attempt_started","shard_id":"s2","test_id":"billing.refund","attempt_id":"billing.refund#1","attempt_no":1},
  {"event_id":"refund-a1-pass","type":"attempt_result","shard_id":"s2","test_id":"billing.refund","attempt_id":"billing.refund#1","attempt_no":1,"result":"passed"}
]}
JSON

# executor s2 crashes (nonzero)
curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"s2-crash","type":"shard_finished","shard_id":"s2","outcome":"crashed","exit_code":137,"error":"lost worker"}]}' >/dev/null

# s3 cancel path
curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' -d @- >/dev/null <<JSON
{"events":[
  {"event_id":"s3-start","type":"shard_started","shard_id":"s3"},
  {"event_id":"export-a1-start","type":"attempt_started","shard_id":"s3","test_id":"reports.export","attempt_id":"reports.export#1","attempt_no":1}
]}
JSON
curl -sf -X POST "${BASE}/v1/runs/${RA}/cancel" -H 'Content-Type: application/json' -d '{}' >/dev/null
curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"s3-cancel","type":"shard_finished","shard_id":"s3","outcome":"canceled"}]}' >/dev/null

# RETRY of charge on recovered shard passes (attempt #2 result delivered LATE)
curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' -d @- >/dev/null <<JSON
{"events":[
  {"event_id":"s2b-start","type":"shard_started","shard_id":"s2"},
  {"event_id":"charge-a2-pass-late","type":"attempt_result","shard_id":"s2","test_id":"billing.charge","attempt_id":"billing.charge#2","attempt_no":2,"result":"passed"},
  {"event_id":"s2b-fin","type":"shard_finished","shard_id":"s2","outcome":"completed","exit_code":0}
]}
JSON

# duplicate redelivery
DUP="$(curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"login-a1-pass","type":"attempt_result","shard_id":"s1","test_id":"auth.login","attempt_id":"auth.login#1","attempt_no":1,"result":"passed"}]}')"
assert_eq "redelivered event is marked duplicate" "true" "$(jqr '.results[0].duplicate' "$DUP")"

# finalize, then a late pass for expired_token must be rejected
curl -sf -X POST "${BASE}/v1/runs/${RA}/finalize" -H 'Content-Type: application/json' -d '{}' >/dev/null
LATE="$(curl -sf -X POST "${BASE}/v1/runs/${RA}/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"expired-late","type":"attempt_result","shard_id":"s1","test_id":"auth.expired_token","attempt_id":"auth.expired_token#1","attempt_no":1,"result":"passed"}]}')"
assert_eq "post-finalize result marked late" "true" "$(jqr '.results[0].late' "$LATE")"

SA="$(curl -sf "${BASE}/v1/runs/${RA}/summary")"
echo "summary A:"; jq . <<<"${SA}" | sed 's/^/    /'

assert_eq "A run failed (crash + missing result)" "failed" "$(jqr '.status' "$SA")"
assert_eq "A auth.login passed" "passed" "$(tstat "$SA" auth.login)"
assert_eq "A billing.charge passed after retry (attempt 2)" "passed" "$(tstat "$SA" billing.charge)"
assert_eq "A billing.refund passed" "passed" "$(tstat "$SA" billing.refund)"
assert_eq "A auth.expired_token incomplete (late pass ignored)" "incomplete" "$(tstat "$SA" auth.expired_token)"
assert_eq "A reports.export canceled" "canceled" "$(tstat "$SA" reports.export)"
assert_eq "A passed count is 4" "4" "$(jqr '.counts.passed' "$SA")"
assert_eq "A incomplete count is 1" "1" "$(jqr '.counts.incomplete' "$SA")"
assert_eq "A canceled count is 1" "1" "$(jqr '.counts.canceled' "$SA")"

SA2="$(curl -sf "${BASE}/v1/runs/${RA}/summary")"
printf '%s' "$SA"  | jq -S . >"${WORK}/a1.json"
printf '%s' "$SA2" | jq -S . >"${WORK}/a2.json"
assert_true "A summary is repeatable across requests" cmp -s "${WORK}/a1.json" "${WORK}/a2.json"

echo
echo "== scenario B: execute mode with real crashing fixture command =="
RB="$(curl -sf -X POST "${BASE}/v1/runs" -H 'Content-Type: application/json' -d @- <<JSON | jq -r '.run_id'
{
  "mode":"execute",
  "shards":[
    {"shard_id":"s-pass","test_ids":["auth.login","auth.logout"]},
    {"shard_id":"s-crash","test_ids":["billing.charge"]}
  ],
  "commands":[
    {"shard_id":"s-pass","command":"bash '${ROOT}/examples/fixtures/shard-pass.sh'"},
    {"shard_id":"s-crash","command":"bash '${ROOT}/examples/fixtures/shard-crash.sh'"}
  ],
  "auto_finalize":true
}
JSON
)"
echo "run B = ${RB}"
SB="$(wait_finalized "$RB")"
echo "summary B:"; jq . <<<"${SB}" | sed 's/^/    /'
sstat() { jqr '.shards[]|select(.shard_id=="'"$2"'").'"$3" "$1"; }
assert_eq "B s-crash recorded as crashed from real exit code" "crashed" "$(sstat "$SB" s-crash status)"
assert_eq "B crashed shard exit code 137" "137" "$(sstat "$SB" s-crash exit_code)"
assert_eq "B billing.charge incomplete (retry died in crash)" "incomplete" "$(tstat "$SB" billing.charge)"
assert_eq "B auth.login passed" "passed" "$(tstat "$SB" auth.login)"
assert_eq "B auth.logout passed" "passed" "$(tstat "$SB" auth.logout)"
assert_eq "B run failed because of crash" "failed" "$(jqr '.status' "$SB")"
assert_true "B fixture wrote no scripts inside the cache dir" bash -c '[ -z "$(find "'"${CACHE}"'" -name "*.sh" 2>/dev/null)" ]'

echo
echo "== scenario C: /replay shuffled + duplicated events are identical =="
RC="$(curl -sf -X POST "${BASE}/v1/replay" -H 'Content-Type: application/json' \
  -d @"${ROOT}/examples/requests/replay-shuffled.json")"
echo "replay: identical=$(jqr '.identical' "$RC") status=$(jqr '.summaries[0].status' "$RC")"
assert_eq "C shuffled replay repeats are identical" "true" "$(jqr '.identical' "$RC")"
rt() { jqr '.summaries[0].tests[]|select(.test_id=="'"$2"'").status' "$1"; }
assert_eq "C replay t1 passed (retry)" "passed" "$(rt "$RC" t1)"
assert_eq "C replay t2 incomplete (missing result)" "incomplete" "$(rt "$RC" t2)"
assert_eq "C replay t3 canceled" "canceled" "$(rt "$RC" t3)"

echo
echo "== scenario D: restart recovery from the on-disk event log =="
kill "${SRV_PID}"; wait "${SRV_PID}" 2>/dev/null || true
"${WORK}/trmerge" -addr "${ADDR}" -cache "${CACHE}" -work "${WDIR}" >"${WORK}/server2.log" 2>&1 &
SRV_PID=$!
wait_health
SD="$(curl -sf "${BASE}/v1/runs/${RA}/summary")"
printf '%s' "$SD" | jq -S . >"${WORK}/ad.json"
assert_true "D summary survives process restart" cmp -s "${WORK}/a1.json" "${WORK}/ad.json"
assert_eq "D finalized flag recovered" "true" "$(jqr '.finalized' "$SD")"
assert_eq "D status recovered" "failed" "$(jqr '.status' "$SD")"

echo
echo "================================================"
echo "  PASS=${pass}  FAIL=${fail}"
echo "================================================"
[ "${fail}" -eq 0 ]
