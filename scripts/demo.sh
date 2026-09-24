#!/usr/bin/env bash
# End-to-end manual demo: starts a real gateway + two synthetic DDS robots,
# sends valid, stale-seq, unauthorised and expired commands, then queries
# status/events and shows the robot-side audit reasons.
#
# Usage:  ./scripts/demo.sh
set -eo pipefail
cd "$(dirname "$0")/.."
source ./setup_env.sh

PORT="${P48_PORT:-8091}"
BASE="http://127.0.0.1:${PORT}"
REG=config/registry.example.json
export P48_PORT="$PORT" P48_REGISTRY="$REG"
LOG_DIR=$(mktemp -d)
echo "logs: $LOG_DIR"

GW_PID=""; ALPHA_PID=""; BRAVO_PID=""
cleanup() {
    for pid in $GW_PID $ALPHA_PID $BRAVO_PID; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    # Give them a moment to shut down, then force any survivors.
    sleep 0.3
    for pid in $GW_PID $ALPHA_PID $BRAVO_PID; do
        [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT

python3 -m p48_gateway.gateway --registry "$REG" --port "$PORT" \
    >"$LOG_DIR/gateway.log" 2>&1 &
GW_PID=$!
python3 -m p48_gateway.robot --registry "$REG" --robot alpha \
    >"$LOG_DIR/alpha.log" 2>&1 &
ALPHA_PID=$!
python3 -m p48_gateway.robot --registry "$REG" --robot bravo \
    >"$LOG_DIR/bravo.log" 2>&1 &
BRAVO_PID=$!
echo "started gateway=$GW_PID alpha=$ALPHA_PID bravo=$BRAVO_PID on port $PORT"

echo "== waiting for stack =="
ready=0
for _ in $(seq 1 60); do
    if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.25
done
[ "$ready" = 1 ] || { echo "gateway never became healthy"; cat "$LOG_DIR"/*.log; exit 1; }
sleep 2  # let DDS discovery + epoch latch settle

# Do not abort the whole demo on expected non-2xx responses.
set +e
{
echo
echo "== 1. valid unicast command -> alpha (expect HTTP 202, applied by alpha) =="
python3 scripts/send_cmd.py command alpha tester_alpha dock 1 '{"pose":[1,2,0]}'

echo
echo "== 2. same target seq regression seq=1 again (expect 409 stale_seq) =="
python3 scripts/send_cmd.py command alpha tester_alpha dock 1 '{"pose":[1,2,0]}'

echo
echo "== 3. cross-space unauthorised tester_bravo -> alpha (expect 403) =="
python3 scripts/send_cmd.py command alpha tester_bravo dock 1 '{}'

echo
echo "== 4. guest tester with no grants -> bravo (expect 403) =="
python3 scripts/send_cmd.py command bravo tester_guest dock 1 '{}'

echo
echo "== 5. unregistered robot id ghost (expect 403/404) =="
python3 scripts/send_cmd.py command ghost tester_alpha dock 1 '{}'

echo
echo "== 6. valid command -> bravo (same topic name cmd, other namespace) =="
python3 scripts/send_cmd.py command bravo tester_alpha dock 1 '{"speed":0.4}'

sleep 1
echo
echo "== 7. alpha status (online + applied_count) =="
python3 scripts/send_cmd.py status alpha tester_alpha

echo
echo "== 8. alpha event audit (carries rejection/accept reasons) =="
python3 scripts/send_cmd.py events alpha tester_alpha

echo
echo "== 9. query isolation: tester_bravo reading alpha events (expect 403) =="
python3 scripts/send_cmd.py events alpha tester_bravo
} 2>&1 | tee "$LOG_DIR/demo.out"
set -e

echo
echo "DEMO COMPLETE. Output and logs in: $LOG_DIR"