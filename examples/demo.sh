#!/usr/bin/env bash
# demo.sh — end-to-end walk-through with synthetic data driven by the virtual
# clock. It starts the server in a temp directory, exercises the full state
# machine (jitter near the threshold, sustained firing/recovery, duplicate
# samples, long no-data gap and out-of-order samples), and prints states plus
# notification events at each step.
set -euo pipefail

BASE="${ALERTFSM_BASE:-}"
TMP="$(mktemp -d)"
DATA="$TMP/data"
BIN="$TMP/alertfsm"
trap 'kill ${SERVER_PID:-} 2>/dev/null || true; rm -rf "$TMP"' EXIT

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo ">> building..."
(cd "$ROOT" && go build -o "$BIN" ./cmd/alertfsm)

# Pick a free TCP port so the demo does not collide with other services.
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
BASE="${ALERTFSM_BASE:-http://127.0.0.1:$PORT}"

echo ">> starting server on $BASE (data dir: $DATA)"
"$BIN" -addr "127.0.0.1:$PORT" -data "$DATA" >"$TMP/server.log" 2>&1 &
SERVER_PID=$!

# wait for readiness
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done

j() { python3 -m json.tool 2>/dev/null || cat; }
req() { curl -s "$@"; }

echo; echo "=== 1. base virtual clock ==="
req "$BASE/api/v1/clock" | j
CLOCK=$(req "$BASE/api/v1/clock" | python3 -c 'import sys,json;print(json.load(sys.stdin)["clock_ms"])')

t=$CLOCK
rule='{
  "id": "cpu-high",
  "metric": "cpu.usage",
  "threshold": 80,
  "direction": "above",
  "pending_for": "60s",
  "recovery_for": "30s",
  "no_data_for": "120s"
}'

echo; echo "=== 2. create rule (80, pending 60s, recovery 30s, no_data 120s) ==="
req -X POST "$BASE/api/v1/rules" -H 'Content-Type: application/json' -d "$rule" | j

send() { # ts value
  req -X POST "$BASE/api/v1/ingest" -H 'Content-Type: application/json' \
    -d "{\"samples\":[{\"metric\":\"cpu.usage\",\"ts_ms\":$1,\"value\":$2}]}"
}
tick() { req -X POST "$BASE/api/v1/admin/tick" -H 'Content-Type: application/json' -d "{\"to_ms\":$1}" >/dev/null; }
state() { req "$BASE/api/v1/rules/cpu-high/state" | STATE_JSON="$(cat)" python3 -c '
import os, json
s = json.loads(os.environ["STATE_JSON"])
print("  status=%-10s last_value=%s last_seen=%s" % (s["status"], s["last_value"], s["last_seen_ms"]))
'; }

echo; echo "=== 3. jitter around the threshold (must NOT fire) ==="
for i in 1 2 3; do
  t=$((t+20000)); echo "-- t=$t value=90 (breach)";  send "$t" 90 >/dev/null; state
  t=$((t+20000)); echo "-- t=$t value=50 (recovers)"; send "$t" 50 >/dev/null; state
done
echo "   events so far: $(req "$BASE/api/v1/events?rule_id=cpu-high" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["events"]))') (expect 0)"

echo; echo "=== 4. sustained breach -> firing ==="
t=$((t+20000)); echo "-- t=$t value=95 breach, pending run starts"; send "$t" 95 >/dev/null; state
t=$((t+60000));  echo "-- tick to t=$t (60s sustained)"; tick "$t"; state
echo "   events:"; req "$BASE/api/v1/events?rule_id=cpu-high" | j

echo; echo "=== 5. duplicate samples do not accumulate duration ==="
DUP_TS=$((CLOCK+140000)) # a timestamp that was actually ingested (value 95)
echo "-- re-send t=$DUP_TS twice (different values, same timestamp)"
send "$DUP_TS" 95 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  duplicate:",[x["reason"] for x in d["duplicate"]])'
send "$DUP_TS" 1  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  duplicate:",[x["reason"] for x in d["duplicate"]])'
state

echo; echo "=== 6. recovery hysteresis: flap once, then sustained recovery ==="
t=$((t+10000)); echo "-- t=$t value=20 good -> recovering"; send "$t" 20 >/dev/null; state
t=$((t+20000)); echo "-- t=$t value=99 flap -> alerting (no new event)"; send "$t" 99 >/dev/null; state
t=$((t+5000));  echo "-- t=$t value=20 good -> recovering again"; send "$t" 20 >/dev/null; state
t=$((t+30000)); echo "-- tick to t=$t (30s recovered)"; tick "$t"; state

echo; echo "=== 7. long no-data gap -> no_data, then resume ==="
t=$((t+120000)); echo "-- tick to t=$t (120s silence)"; tick "$t"; state
t=$((t+60000));  echo "-- tick to t=$t (even longer silence, no extra events)"; tick "$t"; state
t=$((t+10000));  echo "-- t=$t data resumes, value=50"; send "$t" 50 >/dev/null; state

echo; echo "=== 8. out-of-order sample: stored, ignored by the FSM ==="
late=$((CLOCK+1000))
send "$late" 100 | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  late:",[x["reason"] for x in d["late"]])'
state
echo "   late sample is queryable:"
req "$BASE/api/v1/samples?metric=cpu.usage&from_ms=$late&to_ms=$late" | j

echo; echo "=== 9. config change explicitly resets state ==="
t=$((t+10000)); send "$t" 99 >/dev/null; tick "$((t+60000))"; state
req -X PUT "$BASE/api/v1/rules/cpu-high" -H 'Content-Type: application/json' \
  -d '{"id":"cpu-high","metric":"cpu.usage","threshold":99.5,"direction":"above","pending_for":"60s","recovery_for":"30s","no_data_for":"120s"}' >/dev/null
echo "-- after update:"; state
echo "   last event:"; req "$BASE/api/v1/events?rule_id=cpu-high&limit=1" | j

echo; echo "=== 10. final event list (events only on transitions) ==="
req "$BASE/api/v1/events?rule_id=cpu-high" | python3 -c '
import sys, json
for e in json.load(sys.stdin)["events"]:
    print("  #%-2d t=%s %-14s %s -> %s: %s" % (e["id"], e["ts_ms"], e["type"], e["from"], e["to"], e["message"]))'

echo; echo ">> demo finished; server log available at $TMP/server.log"
