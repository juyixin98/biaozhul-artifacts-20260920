#!/usr/bin/env bash
# End-to-end curl demonstration of the acceptance scenarios:
#   1. slow head (timeout-sized work) blocks later settled events, then drains in order
#   2. retry exhaustion commits a FAILED placeholder in position
#   3. in-flight buffer cap rejects overflow
#   4. cancelling a buffered event means its result is never submitted
#
# Usage: scripts/demo.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-18080}"
BASE="http://localhost:$PORT"
JAVA="${JAVA:-java}"
JAVAC="${JAVAC:-javac}"

mkdir -p target/classes
find src/main/java -name '*.java' > target/sources.txt
"$JAVAC" -encoding UTF-8 -d target/classes @target/sources.txt

"$JAVA" -cp target/classes orderedevents.Main \
  --port "$PORT" --max-in-flight 3 --max-concurrent 2 \
  --default-timeout-ms 400 --retry-backoff-ms 30 > target/demo-server.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

# Wait for /health (fails loudly if some other process already owns the port).
UP=
for _ in $(seq 1 50); do
  BODY="$(curl -sf "$BASE/health" 2>/dev/null || true)"
  if echo "$BODY" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    UP=1
    break
  fi
  if ! kill -0 "$SRV" 2>/dev/null; then
    echo "server process exited during startup; see target/demo-server.log" >&2
    cat target/demo-server.log >&2
    exit 1
  fi
  sleep 0.1
done
if [ -z "$UP" ]; then
  echo "service did not come up on port $PORT (port busy or blocked)." >&2
  echo "retry with a free port: scripts/demo.sh 19090" >&2
  exit 1
fi

j() { jq -c .; }

echo "==================================================================="
echo "1) ORDERED COMMIT — head hangs past its deadline; later events win"
echo "   the race to finish but cannot be committed before the head."
echo "==================================================================="
H=$(curl -s -X POST "$BASE/partitions/demo-order/events" -H 'Content-Type: application/json' \
  -d '{"payload":"head","attempts":[{"delayMillis":5000,"behavior":"timeout"}],"maxAttempts":1}')
echo "submit head : $H" | cut -c1-160
M=$(curl -s -X POST "$BASE/partitions/demo-order/events" -H 'Content-Type: application/json' \
  -d '{"payload":"middle","attempts":[{"delayMillis":50,"behavior":"succeed"}]}')
echo "submit middle (50ms, succeeds early)"
T=$(curl -s -X POST "$BASE/partitions/demo-order/events" -H 'Content-Type: application/json' \
  -d '{"payload":"tail","attempts":[{"delayMillis":80,"behavior":"succeed"}]}')
echo "submit tail   (80ms, succeeds early)"
echo
echo "waiting on ordered output (long-poll) ..."
curl -s "$BASE/partitions/demo-order/results?afterSeq=-1&waitForSeq=2&waitMillis=5000" | jq '.results[] | {seq,status,value,error,attempts,durationMillis}'

echo
echo "==================================================================="
echo "2) RETRY EXHAUSTION — 2 failing attempts commit a FAILED placeholder"
echo "   exactly at the event's input position; success behind it follows."
echo "==================================================================="
curl -s -X POST "$BASE/partitions/demo-retry/events" -H 'Content-Type: application/json' \
  -d '{"payload":"x","attempts":[{"delayMillis":20,"behavior":"fail","error":"boom"}],"maxAttempts":2}' >/dev/null
curl -s -X POST "$BASE/partitions/demo-retry/events" -H 'Content-Type: application/json' \
  -d '{"payload":"y","attempts":[{"delayMillis":10,"behavior":"succeed"}]}' >/dev/null
sleep 1.5
curl -s "$BASE/partitions/demo-retry/results" | jq '.results[] | {seq,status,value,error,attempts}'

echo
echo "==================================================================="
echo "3) BUFFER CAP — maxInFlight=3; the 4th and 5th submits get HTTP 503"
echo "==================================================================="
for i in 1 2 3 4 5; do
  CODE=$(curl -s -o /tmp/demo-cap.json -w '%{http_code}' -X POST "$BASE/partitions/demo-cap/events" \
    -H 'Content-Type: application/json' \
    -d "{\"payload\":\"e$i\",\"attempts\":[{\"delayMillis\":1500,\"behavior\":\"succeed\"}],\"timeoutMillis\":3000}")
  echo "submit #$i -> HTTP $CODE $( [ "$CODE" != 202 ] && jq -c '.error' /tmp/demo-cap.json || true)"
done
curl -s "$BASE/partitions/demo-cap/status" | jq .

echo
echo "==================================================================="
echo "4) CANCELLATION — occupy both concurrency slots; cancel the buffered"
echo "   3rd event; only the two occupants commit, the cancelled seq=2 is"
echo "   skipped and never appears in the output."
echo "==================================================================="
curl -s -X POST "$BASE/partitions/demo-cancel/events" -H 'Content-Type: application/json' \
  -d '{"payload":"occupy-1","attempts":[{"delayMillis":1200,"behavior":"succeed"}],"timeoutMillis":3000}' >/dev/null
curl -s -X POST "$BASE/partitions/demo-cancel/events" -H 'Content-Type: application/json' \
  -d '{"payload":"occupy-2","attempts":[{"delayMillis":1200,"behavior":"succeed"}],"timeoutMillis":3000}' >/dev/null
B=$(curl -s -X POST "$BASE/partitions/demo-cancel/events" -H 'Content-Type: application/json' \
  -d '{"payload":"buffered","attempts":[{"delayMillis":0,"behavior":"succeed"}],"timeoutMillis":3000}')
BID=$(echo "$B" | jq -r .id)
echo "buffered event: $BID (state before cancel: $(echo "$B" | jq -r .state))"
echo "cancel ->"
curl -s -X POST "$BASE/partitions/demo-cancel/events/$BID/cancel" | jq '{id,state,seq,error}'
echo "ordered output after occupants finish (note the seq gap 0,1 and no $BID):"
curl -s "$BASE/partitions/demo-cancel/results?afterSeq=-1&waitForSeq=1&waitMillis=8000" \
  | jq '{count,lastSeq,results:[.results[]|{seq,status,value}]}'

echo
echo "demo finished; server log saved to target/demo-server.log"
