#!/usr/bin/env bash
# End-to-end acceptance demo against a running watchdog-host in manual-clock
# mode. Start the server first:
#
#   cargo run --release -- --clock manual --db /tmp/wd-demo.db --bind 127.0.0.1:8080
#
# then run:  bash examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
JQ="${JQ:-jq}"

req() { # method path [json]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H 'content-type: application/json' -d "$body" "$BASE$path"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
}

say() { printf '\n=== %s ===\n' "$*"; }

say "1. initial status (fresh device, generation 0)"
req GET /status | $JQ '{safe_mode: .state.safe_mode, gen: .state.fault_generation, resets: .state.consecutive_resets}'

say "2. all three tasks report progress, feed accepted"
req POST /tasks/control-loop/heartbeat '{"counter": 1}' | $JQ -c .result
req POST /tasks/sensor-fusion/heartbeat '{"counter": 1}' | $JQ -c .result
req POST /tasks/logger/heartbeat        '{"counter": 1}' | $JQ -c .result
req POST /feed | $JQ -c .

say "3. repeated heartbeats with the SAME counter are not progress -> feed rejected"
req POST /tasks/control-loop/heartbeat '{"counter": 1}' | $JQ -c .result
req POST /tasks/sensor-fusion/heartbeat '{"counter": 1}' | $JQ -c .result
req POST /tasks/logger/heartbeat        '{"counter": 1}' | $JQ -c .result
req POST /feed | $JQ -c .

say "4. logger gets stuck; three expired windows -> safe mode"
for i in 1 2 3; do
  # healthy tasks keep making progress inside the window...
  req POST /tasks/control-loop/heartbeat "{\"counter\": $((10 + i * 2))}" > /dev/null
  req POST /tasks/sensor-fusion/heartbeat "{\"counter\": $((20 + i * 2))}" > /dev/null
  req POST /clock/advance '{"delta_ms": 4000}' > /dev/null
  req POST /tasks/control-loop/heartbeat "{\"counter\": $((11 + i * 2))}" > /dev/null
  req POST /tasks/sensor-fusion/heartbeat "{\"counter\": $((21 + i * 2))}" > /dev/null
  # ...but the window expires with logger still silent
  req POST /clock/advance '{"delta_ms": 1001}' > /dev/null
  req POST /tick | $JQ -c '.outcome'
done
req GET /status | $JQ '{safe_mode: .state.safe_mode, gen: .state.fault_generation, resets: .state.consecutive_resets, reason: .state.last_reset_reason}'

say "5. heartbeats keep flowing but safe mode is NOT cleared"
req POST /tasks/logger/heartbeat '{"counter": 99}' | $JQ -c '{progressed: .result.progressed, safe_mode: .safe_mode}'
req POST /feed | $JQ -c .

say "6. stale clear (generation 0) rejected; correct generation clears"
req POST /safe-mode/clear '{"fault_generation": 0}' | $JQ -c .
req POST /safe-mode/clear '{"fault_generation": 1}' | $JQ -c .

say "7. durable reset journal (reasons + last progress snapshots)"
req GET /resets | $JQ '.resets[] | {at_ms, fault_generation, consecutive_resets, reason}'

say "8. restart persistence: stop the server (Ctrl-C), start it again with the same --db, then:"
echo "   curl -s $BASE/status | jq '{safe_mode: .state.safe_mode, gen: .state.fault_generation}'"
echo "   (safe_mode would still be true if the clear in step 6 had not run)"
