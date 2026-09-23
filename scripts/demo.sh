#!/usr/bin/env bash
# End-to-end demo against a real running server (manual-time mode so windows are
# driven deterministically via /v1/admin/advance-time).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-0}"
if [ "$PORT" = "0" ]; then
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
fi
BASE="http://localhost:${PORT}"
echo "Using port ${PORT}"

mkdir -p build/classes docs
find src -name '*.java' > build/sources.txt
javac -d build/classes @build/sources.txt

java -cp build/classes approx.Main --port "$PORT" --manual-time > docs/server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for the port.
for _ in $(seq 1 50); do
  if curl -sf "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

j() { python3 -m json.tool; }

echo "== 1. health =="
curl -s "$BASE/healthz" | j

echo "== 2. create engine (epsilon/delta sizing, 1000ms windows, exact on) =="
curl -s -X POST "$BASE/v1/engines" -H 'Content-Type: application/json' \
  -d '{"id":"demo","epsilon":0.05,"delta":0.01,"candidateCapacity":5,"windowMillis":1000,"trackExact":true}' | j

echo "== 3. send a skewed batch into window 0 =="
# timestampMillis pins every event to window [0,1000) regardless of virtual "now"
python3 -c '
import json
body = json.load(open("examples/events.json"))
for e in body["events"]:
    if isinstance(e, dict):
        e.pop("timestampMillis", None)
body["timestampMillis"] = 100
print(json.dumps(body))
' > /tmp/events-pinned.json
curl -s -X POST "$BASE/v1/engines/demo/events" -H 'Content-Type: application/json' \
  --data @/tmp/events-pinned.json | j

echo "== 4. point query: apple (estimate + declared bound + true count) =="
curl -s "$BASE/v1/engines/demo/items/apple" | j

echo "== 5. candidate top-K (coverage NOT guaranteed, count bound declared) =="
curl -s "$BASE/v1/engines/demo/topk?k=5" | j

echo "== 6. advance virtual time past the window boundary =="
curl -s -X POST "$BASE/v1/admin/advance-time" -H 'Content-Type: application/json' \
  -d '{"millis":2000}' | j

echo "== 7. inspect the closed window (ground truth via exactTopK) =="
curl -s "$BASE/v1/engines/demo/windows/0" | j

echo "== 8. merge: export another engine's sketch and merge the compatible one =="
curl -s -X POST "$BASE/v1/engines" -H 'Content-Type: application/json' \
  -d '{"id":"other","width":55,"depth":5,"seed":42,"candidateCapacity":5}' >/dev/null
curl -s "$BASE/v1/engines/other/sketch" > /tmp/other-sketch.json
curl -s -X POST "$BASE/v1/engines/other/events" -H 'Content-Type: application/json' \
  -d '{"events":[{"item":"apple","count":7}]}' >/dev/null
# compatible: same width/depth/seed
curl -s "$BASE/v1/engines/other/sketch" > /tmp/other-sketch.json
python3 -c 'import json; print(json.dumps({"sketch": json.load(open("/tmp/other-sketch.json"))}))' \
  > /tmp/merge-body.json
curl -s -X POST "$BASE/v1/engines/other/merge" -H 'Content-Type: application/json' \
  --data @/tmp/merge-body.json | j

echo "== 9. incompatible merges are rejected (409) =="
curl -s -X POST "$BASE/v1/engines" -H 'Content-Type: application/json' \
  -d '{"id":"baddiff","width":55,"depth":5,"seed":43,"candidateCapacity":5}' >/dev/null
curl -s -o /tmp/resp-seed.json -w "different seed -> HTTP %{http_code}\n" \
  -X POST "$BASE/v1/engines/other/merge" -H 'Content-Type: application/json' \
  -d "{\"sketch\":$(curl -s "$BASE/v1/engines/baddiff/sketch")}"
cat /tmp/resp-seed.json | j

curl -s -X POST "$BASE/v1/engines" -H 'Content-Type: application/json' \
  -d '{"id":"badwidth","width":56,"depth":5,"seed":42,"candidateCapacity":5}' >/dev/null
curl -s -o /tmp/resp-width.json -w "different width -> HTTP %{http_code}\n" \
  -X POST "$BASE/v1/engines/other/merge" -H 'Content-Type: application/json' \
  -d "{\"sketch\":$(curl -s "$BASE/v1/engines/badwidth/sketch")}"
cat /tmp/resp-width.json | j

echo
echo "Demo complete. Server log: docs/server.log"
