#!/usr/bin/env bash
# End-to-end acceptance test for the scaling stable-window service.
# Starts the real binary with a MANUAL clock, drives step load, a scale-down
# blocked by the stable window, the window release, data-quality cases,
# event-time regression, max-replica cap, and verifies the HMAC signature
# with a real cryptographic check.
set -euo pipefail

cd "$(dirname "$0")/.."

ADDR="127.0.0.1:18101"
BASE="http://${ADDR}"
DB="$(mktemp -d)/accept.db"
KEY_HEX="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
T0="2026-09-23T12:00:00Z"
SCALER="demo"

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
check(){ if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (got '$1' want '$2')"; fi; }

echo "== build =="
go build -o /tmp/scalerd ./cmd/scalerd
go build -o /tmp/verify  ./cmd/verify

echo "== start server (manual clock at $T0) =="
SCALER_SIGNING_KEY="$KEY_HEX" /tmp/scalerd -addr="$ADDR" -db="$DB" \
  -manual-clock -clock-start="$T0" >/tmp/scalerd.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null && break
  sleep 0.1
done
curl -sf "$BASE/healthz" >/dev/null || { echo "server did not start"; cat /tmp/scalerd.log; exit 1; }
ok "server healthy"

api() { # METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -X "$method" -H 'Content-Type: application/json' -d "$body" "$BASE$path"
  else
    curl -s -X "$method" "$BASE$path"
  fi
}
jqf() { jq -r "$2" <<<"$1"; }  # json filter

echo "== config =="
R=$(api PUT "/scalers/$SCALER/config" "$(cat examples/config.json)")
check "$(jqf "$R" '.version')" "1" "config created as version 1"
FP1="$(jqf "$R" '.fingerprint')"
[ "${#FP1}" = "64" ] && ok "config fingerprint is SHA-256 (64 hex)" || bad "fingerprint length"

R=$(api PUT "/scalers/$SCALER/config" '{"targetUtilization":0,"minReplicas":1,"maxReplicas":3}')
code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' \
  -d '{"targetUtilization":0,"minReplicas":1,"maxReplicas":3}' "$BASE/scalers/$SCALER/config")
check "$code" "422" "zero target utilization rejected (no division by zero)"

echo "== clock control =="
check "$(jqf "$(api GET /clock)" '.manual')" "true" "clock is manual"
api POST /clock/advance '{"seconds":30}' >/dev/null
check "$(jqf "$(api GET /clock)" '.now')" "2026-09-23T12:00:30Z" "clock advanced to 12:00:30"

pods() { # N UTIL -> JSON array of ready instances
  local n="$1" util="$2" out="[" i
  for ((i=0;i<n;i++)); do
    [ "$i" -gt 0 ] && out+=","
    out+="{\"name\":\"p$i\",\"ready\":true,\"utilizationPct\":$util}"
  done
  echo "$out]"
}
missingPods() { # N -> JSON array of ready instances with NO metric field (not zero!)
  local n="$1" out="[" i
  for ((i=0;i<n;i++)); do
    [ "$i" -gt 0 ] && out+=","
    out+="{\"name\":\"p$i\",\"ready\":true}"
  done
  echo "$out]"
}
decide() { # REPLICAS INSTANCES_JSON -> decision JSON
  api POST "/scalers/$SCALER/decide" "$(jq -n \
    --argjson cur "$1" --argjson inst "$2" --arg ts "$(jqf "$(api GET /clock)" '.now')" \
    '{configVersion:1,currentReplicas:$cur,metricTimestamp:$ts,instances:$inst}')"
}

echo "== step load up (4x100%) =="
R=$(decide 4 "$(pods 4 100)")
check "$(jqf "$R" '.rawDesired')"   "6" "raw recommendation = ceil(4*100/70) = 6"
check "$(jqf "$R" '.finalDesired')" "6" "limited recommendation = 6"
check "$(jqf "$R" '.finalAction')"  "ScaleUp" "action ScaleUp"
check "$(jqf "$R" '.configVersion')" "1" "decision bound to config version"
check "$(jqf "$R" '(.metricTimestamp|length>0)')" "true" "decision bound to metric time"
SIG="$(jqf "$R" '.signature')"; [ "${#SIG}" = "64" ] && ok "decision carries HMAC-SHA256 signature" || bad "signature"
jqf "$R" '.reasons | join(",")' | grep -q raw_scale_up && ok "reason includes raw_scale_up" || bad "reason raw_scale_up"
echo "$R" | /tmp/verify -key-hex="$KEY_HEX" >/tmp/verify.out && ok "HMAC signature verifies with real key" || bad "HMAC verification"
cat /tmp/verify.out | sed 's/^/    /'
echo "$R" | jq '.finalDesired=999' | /tmp/verify -key-hex="$KEY_HEX" >/dev/null 2>&1 \
  && bad "tampered decision wrongly verifies" || ok "tampered decision rejected by HMAC"
echo "$R" | /tmp/verify -key-hex="$(printf '%064d' 0)" >/dev/null 2>&1 \
  && bad "wrong key wrongly verifies" || ok "wrong signing key rejected"

echo "== scale-down blocked by stable window =="
api POST /clock/advance '{"seconds":30}' >/dev/null   # 12:01:00
R=$(decide 6 "$(pods 6 10)")
check "$(jqf "$R" '.rawDesired')"   "1" "raw recommendation now 1"
check "$(jqf "$R" '.finalDesired')" "6" "stable window MAX keeps fleet at 6"
jqf "$R" '.reasons | join(",")' | grep -q scale_down_stable_window_max \
  && ok "reason scale_down_stable_window_max" || bad "window reason"

echo "== missing metrics are never filled with zero =="
# 3 ready pods report 10%, 1 ready pod has NO metric field (missing != 0).
# Missing is assumed 100% for safety: eff=(10+10+10+100)/4=32.5 -> raw 2.
api POST /clock/advance '{"seconds":30}' >/dev/null   # 12:01:30
BODY=$(jq -n --arg ts "$(jqf "$(api GET /clock)" '.now')" '{
  configVersion:1, currentReplicas:4, metricTimestamp:$ts,
  instances:[
    {name:"p0",ready:true,utilizationPct:10},
    {name:"p1",ready:true,utilizationPct:10},
    {name:"p2",ready:true,utilizationPct:10},
    {name:"p3",ready:true}
  ]}')
R=$(api POST "/scalers/$SCALER/decide" "$BODY")
check "$(jqf "$R" '.readyMissing')" "1" "one ready pod marked missing (not 0%)"
check "$(jqf "$R" '.rawDesired')" "2" "missing assumed 100%, not 0 (raw=2 not 1)"

# all ready pods missing -> hold, no recommendation recorded
api POST /clock/advance '{"seconds":30}' >/dev/null   # 12:02:00
BODY=$(jq -n --arg ts "$(jqf "$(api GET /clock)" '.now')" '{
  configVersion:1, currentReplicas:6, metricTimestamp:$ts,
  instances:[{name:"p0",ready:true},{name:"p1",ready:true},{name:"p2",ready:true},
             {name:"p3",ready:true},{name:"p4",ready:true},{name:"p5",ready:true}]}')
R=$(api POST "/scalers/$SCALER/decide" "$BODY")
check "$(jqf "$R" '.finalAction')" "Hold" "all-missing -> Hold"
jqf "$R" '.reasons | join(",")' | grep -q all_ready_instances_missing_metrics \
  && ok "reason all_ready_instances_missing_metrics" || bad "all-missing reason"
check "$(jqf "$R" '.recommendationRecorded')" "false" "missing tick records no recommendation"

echo "== unready instance handled separately =="
api POST /clock/advance '{"seconds":30}' >/dev/null   # 12:02:30
# 6 ready at 100% + 1 not-ready pod (excluded from measured average, weighted
# at 100% during scale-up): eff=100 -> ceil(7*100/70)=10.
BODY=$(jq -n --arg ts "$(jqf "$(api GET /clock)" '.now')" '{
  configVersion:1, currentReplicas:7, metricTimestamp:$ts,
  instances:[
    {name:"p0",ready:true,utilizationPct:100},{name:"p1",ready:true,utilizationPct:100},
    {name:"p2",ready:true,utilizationPct:100},{name:"p3",ready:true,utilizationPct:100},
    {name:"p4",ready:true,utilizationPct:100},{name:"p5",ready:true,utilizationPct:100},
    {name:"p6",ready:false}
  ]}')
R=$(api POST "/scalers/$SCALER/decide" "$BODY")
check "$(jqf "$R" '.unreadyTotal')" "1" "unready pod counted separately"
check "$(jqf "$R" '.rawDesired')" "10" "unready weighted at 100% during scale-up: ceil(7*100/70)=10"

echo "== event-time regression rejected =="
# newest accepted event is 12:02:30; a late event stamped 12:00:30 is refused
# even though its body is malformed (instances missing) — ordering wins.
code=$(curl -s -o /tmp/reg.json -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "$(jq -n '{configVersion:1,currentReplicas:6,
        metricTimestamp:"2026-09-23T12:00:30Z", instances:[]}')" \
  "$BASE/scalers/$SCALER/decide")
check "$code" "409" "older/garbled metric timestamp rejected (409 takes priority)"
grep -q metric_timestamp_regressed /tmp/reg.json && ok "error reason metric_timestamp_regressed" || bad "regression reason"

echo "== idempotent replay at identical event time =="
api POST /clock/advance '{"seconds":30}' >/dev/null   # 12:03:00
TS="$(jqf "$(api GET /clock)" '.now')"
BODY=$(jq -n --arg ts "$TS" '{configVersion:1,currentReplicas:6,metricTimestamp:$ts,
  instances:[{name:"p0",ready:true,utilizationPct:50},{name:"p1",ready:true,utilizationPct:50},
             {name:"p2",ready:true,utilizationPct:50},{name:"p3",ready:true,utilizationPct:50},
             {name:"p4",ready:true,utilizationPct:50},{name:"p5",ready:true,utilizationPct:50}]}')
R1=$(api POST "/scalers/$SCALER/decide" "$BODY")
R2=$(api POST "/scalers/$SCALER/decide" "$BODY")
[ "$(jqf "$R1" '.signature')" = "$(jqf "$R2" '.signature')" ] \
  && ok "identical replay returns the same decision" || bad "idempotent replay differs"

echo "== stable window blocks then releases =="
# High recommendations recorded: 6 @12:00:30, 2 @12:01:30, 10 @12:02:30,
# 8 @12:03:00 (50% util, outside the 10% tolerance band).
# At 12:06:00 low load still sees the window max 10 [12:01:00,12:06:00].
api POST /clock/set '{"now":"2026-09-23T12:06:00Z"}' >/dev/null
R=$(decide 10 "$(pods 10 10)")
check "$(jqf "$R" '.rawDesired')"   "2" "raw = ceil(10*10/70)=2"
check "$(jqf "$R" '.finalDesired')" "10" "stable window MAX still holds 10"
# At 12:08:31 every recommendation (latest 12:06:00) is inside the window
# except... 12:06:00 is 151s old, so window [12:03:31,12:08:31] contains the
# 12:06:00 record (=2) and 8@12:03:00 is excluded: max becomes 2.
api POST /clock/set '{"now":"2026-09-23T12:08:31Z"}' >/dev/null
R=$(decide 10 "$(pods 10 10)")
check "$(jqf "$R" '.rawDesired')"   "2" "raw 2 once window elapsed"
check "$(jqf "$R" '.finalDesired')" "2" "final 2: scale-down executed"
check "$(jqf "$R" '.finalAction')"  "ScaleDown" "action ScaleDown after window"

echo "== config version reset opens a fresh stable window =="
R=$(api PUT "/scalers/$SCALER/config" "$(jq '.maxReplicas=12' examples/config.json)")
check "$(jqf "$R" '.version')" "2" "new config version 2"
[ "$(jqf "$R" '.fingerprint')" != "$FP1" ] && ok "fingerprint changed with policy" || bad "fingerprint"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "$(jq -n '{configVersion:1,currentReplicas:1,
        metricTimestamp:"2026-09-23T12:08:00Z",
        instances:[{name:"p0",ready:true,utilizationPct:70}]}')" \
  "$BASE/scalers/$SCALER/decide")
check "$code" "409" "stale config version refused even with fresh event time"
R=$(api POST "/scalers/$SCALER/decide" "$(jq -n '{configVersion:2,currentReplicas:1,
      metricTimestamp:"2026-09-23T12:09:00Z",
      instances:[{name:"p0",ready:true,utilizationPct:70}]}')")
check "$(jqf "$R" '.configVersion')" "2" "decision recorded against v2"
check "$(jqf "$R" '.windowSamplesConsidered')" "1" "new version's window is isolated (only the v2 tick)"

echo "== stale sample isolated on a fresh scaler (no zero-fill) =="
# Jump forward; the sample is >metricFreshnessSeconds(120s) old. It is held,
# not treated as 0% (which would have scaled the fleet down).
api POST /clock/set '{"now":"2026-09-23T12:12:00Z"}' >/dev/null
api PUT "/scalers/fresh/config" "$(cat examples/config.json)" >/dev/null
code=$(curl -s -o /tmp/stale.json -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -d "$(jq -n '{configVersion:1,currentReplicas:2,metricTimestamp:"2026-09-23T12:09:00Z",
        instances:[{name:"p0",ready:true,utilizationPct:99},{name:"p1",ready:true,utilizationPct:99}]}')" \
  "$BASE/scalers/fresh/decide")
check "$code" "200" "stale sample accepted as an event but held (HTTP 200)"
grep -q metrics_stale /tmp/stale.json && ok "reason metrics_stale (no zero-fill)" || bad "stale reason"

echo
echo "================ RESULT: $PASS passed, $FAIL failed ================"
[ "$FAIL" -eq 0 ]
