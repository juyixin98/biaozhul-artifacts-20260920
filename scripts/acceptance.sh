#!/usr/bin/env bash
# End-to-end acceptance test for the sensor health service.
# Exercises real HMAC-signed HTTP ingestion, SQLite persistence, the three
# detection rules, hysteresis, clock rollback, backfill, restart, and a
# webhook receiver that verifies alert signatures.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-18080}"
WHPORT="${WHPORT:-19090}"
BASE="http://127.0.0.1:${PORT}"
TMP="$(mktemp -d)"
DB="$TMP/accept.db"
INGEST_SECRET="accept-ingest-secret"
ADMIN_TOKEN="accept-admin-token"
WH_SECRET="accept-webhook-secret"
PASS=0; FAIL=0
RECV_PID=""

cleanup() { # stop only the background processes we started
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  [ -n "$RECV_PID" ] && kill "$RECV_PID" 2>/dev/null || true
}
trap cleanup EXIT

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
check(){ if eval "${2:-true}"; then ok "$1"; else bad "$1 (${3:-})"; fi; }

assert_state() { # device rule expected
  local dev="$1" rule="$2" want="$3"
  local got
  got=$(curl -s "$BASE/v1/devices/$dev" | jq -r ".rules.\"$rule\".state")
  check "$dev/$rule state == $want" "[ '$got' = '$want' ]" "got '$got'"
}

server_clock() { curl -s -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE/admin/clock" | jq -r .now; }

# Sign with the *server* clock so virtual-clock advances stay in the window.
post_signed() { # file: prints the response JSON body
  local file="$1"
  local now; now="$(server_clock)"
  local raw
  raw=$(./bin/sensorctl post --url "$BASE" --secret "$INGEST_SECRET" \
    --file "$file" --timestamp "$now")
  # sensorctl prints "HTTP <code>" then the body; strip the status line.
  echo "$raw" | sed '1{/^HTTP /d;}'
}

echo "== build =="
go build -o bin/sensorhealth-server ./cmd/server
go build -o bin/sensorctl ./cmd/sensorctl
ok "binaries build"

echo "== webhook receiver (verifies real HMAC signatures) =="
./bin/sensorctl recv --addr ":$WHPORT" --secret "$WH_SECRET" >"$TMP/receiver.log" 2>&1 &
RECV_PID=$!
sleep 0.5

echo "== start server (virtual clock, SQLite at $DB) =="
./bin/sensorhealth-server --addr ":$PORT" --db "$DB" --clock virtual \
  --ingest-secret "$INGEST_SECRET" --admin-token "$ADMIN_TOKEN" \
  --webhook-url "http://127.0.0.1:$WHPORT/" --webhook-secret "$WH_SECRET" \
  >"$TMP/server.log" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.1; done
check "healthz" "[ '$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")' = 200 ]"

echo "== configure temperature: enter/recover thresholds differ =="
curl -s -X PUT -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data @examples/device_type_temperature.json \
  "$BASE/admin/config/device-types/temperature" > "$TMP/cfg.json"
CFGVER=$(jq -r .version "$TMP/cfg.json")
check "config version stamped" "[ '$CFGVER' -ge 2 ]" "version=$CFGVER"
check "recover window < enter window" \
  "[ $(jq .stale_recover_sec "$TMP/cfg.json") -lt $(jq .stale_enter_sec "$TMP/cfg.json") ]"

# Four identical samples (window 4): fixed_value must fire, stationary
# door contact with the same constant value must not.
cat > "$TMP/fixed.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"temp-1","type":"temperature","seq":1,"sample_time":"2026-09-23T10:00:00Z","value":21.0},
 {"device_id":"temp-1","type":"temperature","seq":2,"sample_time":"2026-09-23T10:00:01Z","value":21.0},
 {"device_id":"temp-1","type":"temperature","seq":3,"sample_time":"2026-09-23T10:00:02Z","value":21.0},
 {"device_id":"temp-1","type":"temperature","seq":4,"sample_time":"2026-09-23T10:00:03Z","value":21.0}
]}
JSON
cat > "$TMP/door.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"door-1","type":"door","seq":1,"sample_time":"2026-09-23T10:00:00Z","value":0},
 {"device_id":"door-1","type":"door","seq":2,"sample_time":"2026-09-23T10:00:01Z","value":0},
 {"device_id":"door-1","type":"door","seq":3,"sample_time":"2026-09-23T10:00:02Z","value":0},
 {"device_id":"door-1","type":"door","seq":4,"sample_time":"2026-09-23T10:00:03Z","value":0},
 {"device_id":"door-1","type":"door","seq":5,"sample_time":"2026-09-23T10:00:04Z","value":0}
]}
JSON
post_signed "$TMP/fixed.json" > "$TMP/r1.json"
post_signed "$TMP/door.json"  > "$TMP/rdoor.json"
check "fixed batch all accepted" "[ $(jq '.accepted' "$TMP/r1.json") = 4 ]"
assert_state temp-1 fixed_value ALERT
assert_state door-1 fixed_value OK

echo "== trigger interval + config version on the event =="
curl -s "$BASE/v1/events?device_id=temp-1&rule=fixed_value&open=true" > "$TMP/ev.json"
check "event start_seq=1" "[ $(jq '.events[0].start_seq' "$TMP/ev.json") = 1 ]"
check "event end_seq=4"   "[ $(jq '.events[0].end_seq'   "$TMP/ev.json") = 4 ]"
check "event carries config version" "[ $(jq '.events[0].config_version' "$TMP/ev.json") = $CFGVER ]"

echo "== same value but normal seq: door type has fixed detection disabled =="
# door-1 already sent 5 constant 0 readings; four more identical, advancing
# sequence samples must still be OK because the door contact is a binary,
# legitimately stationary device (fixed_window_count=0 for its type).
cat > "$TMP/door2.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"door-1","type":"door","seq":6,"sample_time":"2026-09-23T10:00:05Z","value":0},
 {"device_id":"door-1","type":"door","seq":7,"sample_time":"2026-09-23T10:00:06Z","value":0},
 {"device_id":"door-1","type":"door","seq":8,"sample_time":"2026-09-23T10:00:07Z","value":0},
 {"device_id":"door-1","type":"door","seq":9,"sample_time":"2026-09-23T10:00:08Z","value":0}
]}
JSON
post_signed "$TMP/door2.json" >/dev/null
assert_state door-1 fixed_value OK

echo "== fixed recovery needs a genuinely changing value =="
cat > "$TMP/change.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"temp-1","type":"temperature","seq":5,"sample_time":"2026-09-23T10:00:04Z","value":23.5}
]}
JSON
post_signed "$TMP/change.json" >/dev/null
assert_state temp-1 fixed_value OK

echo "== sequence gap: 5 -> 10 missing 6..9; two clean samples recover =="
cat > "$TMP/gap.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"temp-1","type":"temperature","seq":10,"sample_time":"2026-09-23T10:00:05Z","value":23.6}
]}
JSON
post_signed "$TMP/gap.json" >/dev/null
assert_state temp-1 sequence_gap ALERT
curl -s "$BASE/v1/events?device_id=temp-1&rule=sequence_gap&open=true" > "$TMP/gapev.json"
check "gap range 6..9" \
  "[ $(jq '.events[0].gap_start' "$TMP/gapev.json") = 6 ] && [ $(jq '.events[0].gap_end' "$TMP/gapev.json") = 9 ]"
# After recovery (2 gap-free samples, configured threshold) the same event
# row is closed but retains its trigger interval and missing range.
for seq in 11 12; do
  cat > "$TMP/clean.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"temp-1","type":"temperature","seq":$seq,"sample_time":"2026-09-23T10:00:0$((seq-5))Z","value":23.$seq}
]}
JSON
  post_signed "$TMP/clean.json" >/dev/null
done
assert_state temp-1 sequence_gap OK
curl -s "$BASE/v1/events?device_id=temp-1&rule=sequence_gap&open=false" > "$TMP/gapevclosed.json"
check "recovered gap event keeps missing range" \
  "[ $(jq '.events[0].gap_start' "$TMP/gapevclosed.json") = 6 ] && [ $(jq '.events[0].gap_end' "$TMP/gapevclosed.json") = 9 ]"
check "recovered event stamped with recovery version" \
  "[ $(jq '.events[0].recovery_config_version' "$TMP/gapevclosed.json") -ge 1 ]"

echo "== stale: silence window enters, heartbeat recovers (distinct clocks) =="
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"advance_ms":11000}' "$BASE/admin/clock/advance" > "$TMP/adv.json"
check "sweep fired stale" "[ $(jq .stale_alerts_fired "$TMP/adv.json") -ge 1 ]"
assert_state temp-1 stale ALERT
cat > "$TMP/hb.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"temp-1","type":"temperature","sample_time":"2026-09-23T10:00:20Z","is_heartbeat":true}
]}
JSON
post_signed "$TMP/hb.json" >/dev/null
assert_state temp-1 stale OK

echo "== device clock rollback opens a new sequence epoch, not a gap =="
cat > "$TMP/rb.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"rb-1","type":"temperature","seq":100,"sample_time":"2026-09-23T11:00:00Z","value":1},
 {"device_id":"rb-1","type":"temperature","seq":1,  "sample_time":"2026-09-23T09:00:00Z","value":2},
 {"device_id":"rb-1","type":"temperature","seq":2,  "sample_time":"2026-09-23T09:00:01Z","value":3}
]}
JSON
post_signed "$TMP/rb.json" > "$TMP/rbr.json"
check "rollback batch flags new epoch" "[ $(jq '[.items[]|select(.new_epoch==true)]|length' "$TMP/rbr.json") = 1 ]"
check "device epoch == 1" "[ $(curl -s "$BASE/v1/devices/rb-1" | jq .epoch) = 1 ]"
assert_state rb-1 sequence_gap OK

echo "== ordered batch backfill is marked, does not move the HW mark =="
post_signed examples/backfill.json > "$TMP/bf.json"
check "backfill items tagged" "[ $(jq '[.items[]|select(.backfill==true)]|length' "$TMP/bf.json") = 3 ]"

echo "== jitter: tiny backwards step stays in epoch =="
cat > "$TMP/jit.json" <<JSON
{"mode":"live","messages":[
 {"device_id":"jit-1","type":"temperature","seq":1,"sample_time":"2026-09-23T10:00:01Z","value":1},
 {"device_id":"jit-1","type":"temperature","seq":2,"sample_time":"2026-09-23T10:00:02Z","value":2},
 {"device_id":"jit-1","type":"temperature","seq":3,"sample_time":"2026-09-23T10:00:01.700Z","value":3}
]}
JSON
post_signed "$TMP/jit.json" >/dev/null
check "jitter keeps epoch 0" "[ $(curl -s "$BASE/v1/devices/jit-1" | jq .epoch) = 0 ]"

echo "== auth: unsigned / wrongly signed ingestion rejected =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  --data @examples/normal.json "$BASE/v1/ingest")
check "unsigned -> 401" "[ '$code' = 401 ]"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
  -H "X-Timestamp: $(server_clock)" -H 'X-Signature: sha256=deadbeef' \
  --data @examples/normal.json "$BASE/v1/ingest")
check "bad signature -> 401" "[ '$code' = 401 ]"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "$BASE/admin/clock/advance" -d '{}')
check "admin without token -> 401" "[ '$code' = 401 ]"

echo "== webhook delivered a signature the receiver verified =="
sleep 0.5
check "receiver accepted a valid HMAC webhook" "grep -q '\[OK\].*fixed_value' '$TMP/receiver.log'"
check "receiver never logged a rejected signature" "! grep -q '\[REJECT\]' '$TMP/receiver.log'"

echo "== restart: kill, reopen same SQLite file, state persists =="
kill "$SRV_PID" 2>/dev/null || true
wait "$SRV_PID" 2>/dev/null || true
sleep 0.5
./bin/sensorhealth-server --addr ":$PORT" --db "$DB" --clock virtual \
  --ingest-secret "$INGEST_SECRET" --admin-token "$ADMIN_TOKEN" >"$TMP/server2.log" 2>&1 &
SRV_PID=$!
for _ in $(seq 1 50); do curl -sf "$BASE/healthz" >/dev/null && break; sleep 0.1; done
EVCOUNT=$(curl -s "$BASE/v1/events?device_id=temp-1" | jq '[.events[]]|length')
check "events survive restart ($EVCOUNT rows)" "[ '$EVCOUNT' -ge 3 ]"
check "device survives restart" "[ '$(curl -s "$BASE/v1/devices/rb-1" | jq -r .device_id)' = rb-1 ]"
check "epoch survives restart" "[ $(curl -s "$BASE/v1/devices/rb-1" | jq .epoch) = 1 ]"

echo
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mALL %d CHECKS PASSED\033[0m\n' "$PASS"
  echo "artifacts: $TMP"
  exit 0
fi
printf '\033[31m%d/%d CHECKS FAILED\033[0m\n' "$FAIL" "$((PASS+FAIL))"
echo "server log: $TMP/server.log"; echo "receiver log: $TMP/receiver.log"
exit 1
