#!/usr/bin/env bash
# End-to-end smoke test: builds, starts the server on an ephemeral port,
# calls every route with curl, and checks key figures. Used to produce
# results/server-smoke.txt.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$ROOT/scripts/build.sh" >/dev/null

LOG="$(mktemp)"
java -cp "$ROOT/out/classes" neardup.Main serve 0 >"$LOG" 2>&1 &
PID=$!

cleanup() {
  kill "$PID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT

# Wait for the chosen port.
for _ in $(seq 1 50); do
  PORT=$(grep -o '127.0.0.1:[0-9]*' "$LOG" | head -1 | cut -d: -f2 || true)
  [ -n "${PORT:-}" ] && break
  sleep 0.1
done
BASE="http://127.0.0.1:$PORT"
echo "server ephemeral base: $BASE"

echo "--- GET /health ---"
curl -s "$BASE/health"
echo

echo "--- GET /corpus (size field) ---"
curl -s "$BASE/corpus" | python3 -c "import json,sys; print('size =', json.load(sys.stdin)['size'])"

echo "--- POST /cluster {} (stats) ---"
curl -s -X POST "$BASE/cluster" -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['stats'], indent=2))"

echo "--- POST /cluster with custom texts ---"
curl -s -X POST "$BASE/cluster" -H 'Content-Type: application/json' \
  -d @"$ROOT/samples/cluster-custom-texts.json" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps(d['stats'], indent=2))
print('clusters:', [[m['index'] for m in c['members']] for c in d['clusters']])
"

echo "--- malformed JSON status ---"
curl -s -o /dev/null -w 'http_status=%{http_code}\n' -X POST "$BASE/cluster" -d '{bad'
