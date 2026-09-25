#!/usr/bin/env bash
# End-to-end demo: starts the JSON service in manual deterministic mode and
# walks the four acceptance scenarios with curl. Requires JDK + curl.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-18080}"
STATE="build/demo-state.json"
BASE="http://localhost:${PORT}"

./build.sh

rm -f "$STATE"
java -cp build/classes com.example.dedup.Main \
  --port "$PORT" --mode manual \
  --window-ms 10000 --lateness-ms 1000 \
  --retention-ms 12000 --tombstone-ttl-ms 12000 \
  --snapshot-file "$STATE" &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

# wait for the port
READY=0
for i in $(seq 1 50); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then READY=1; break; fi
  sleep 0.1
done
if [ "$READY" != "1" ]; then
  echo "service did not come up on port $PORT (is it already in use?)" >&2
  exit 1
fi

j() { python3 -m json.tool 2>/dev/null || cat; }

echo; echo "=== health ==="
curl -s "$BASE/health" | j

echo; echo "=== scenario 1: basic ingest incl. duplicate (evt-1001 twice) ==="
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @examples/01-events-basic.json | j

echo; echo "=== scenario 1b: same id with DIFFERENT payload + event-time skew ==="
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @examples/02-same-id-different-payload.json | j

echo; echo "=== scenario 2: processing clock rollback (/tick backwards); dedup unaffected ==="
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @examples/04-late-duplicate-step1.json | j
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' --data @examples/08-tick.json | j
curl -s -X POST "$BASE/tick" -H 'Content-Type: application/json' \
  --data '{"advanceMillis":-5000}' | j
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @examples/05-late-duplicate-step2-replay.json | j

echo; echo "=== scenario 3: tombstones (DELETE hides earlier upsert; re-create passes) ==="
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @examples/03-tombstone.json | j

echo; echo "=== fire windows (watermark -> 12000) then drain ==="
curl -s -X POST "$BASE/watermark" -H 'Content-Type: application/json' \
  --data @examples/07-fire-windows.json | j
curl -s -X POST "$BASE/drain" -H 'Content-Type: application/json' --data '{}' | j

echo; echo "=== snapshot for restart recovery ==="
curl -s -X POST "$BASE/snapshot" -H 'Content-Type: application/json' --data '{}' | j

echo; echo "=== scenario 4: extremely late duplicate after far watermark advance ==="
curl -s -X POST "$BASE/watermark" -H 'Content-Type: application/json' \
  --data @examples/06-advance-watermark-far.json | j
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  --data @examples/05-late-duplicate-step2-replay.json | j

echo; echo "=== final metrics ==="
curl -s "$BASE/metrics" | j

echo; echo "(demo service stopping)"
