#!/usr/bin/env bash
# Reproducible end-to-end demo of the HLC JSON backend over HTTP using the fixed
# request files in samples/. Requires: a built tree (scripts/build.sh) and curl.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${HLC_PORT:-0}"   # 0 = let the OS assign a free port (avoids collisions on shared hosts)
STATE="data/http-demo.properties"
LOG="build/http-demo.log"

echo "[demo] building"
scripts/build.sh >/dev/null

echo "[demo] starting server (requested port $PORT, state=$STATE)"
rm -f "$STATE" "$LOG"
java -Dport="$PORT" -DstateFile="$STATE" -cp build/classes hlc.server.HLCHttpServer \
  > "$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

# Wait for the chosen port to be announced, then read it back.
for _ in $(seq 1 50); do
  PORT=$(grep -oE 'localhost:[0-9]+' "$LOG" 2>/dev/null | head -1 | cut -d: -f2 || true)
  [ -n "$PORT" ] && break
  sleep 0.1
done
[ -n "$PORT" ] || { echo "[demo] server failed to start:"; cat "$LOG"; exit 1; }
BASE="http://localhost:$PORT"
echo "[demo] server up on $BASE"

req() { # method path [json-file]
  local method="$1" path="$2" file="${3:-}"
  echo "================ $method $path $file ================"
  if [ -n "$file" ]; then
    curl -s -X "$method" "$BASE$path" -H 'Content-Type: application/json' --data @"$file"
  else
    curl -s -X "$method" "$BASE$path" -H 'Content-Type: application/json'
  fi
  echo
}

req GET  /health
req POST /nodes   samples/01-create-node.json
req POST /nodes   <(echo '{"node":"bob"}')
req POST /tick    samples/02-tick.json
req POST /send    samples/03-send.json
req POST /receive samples/04-receive.json
req POST /persist/save
echo "---- persisted state file ----"
cat "$STATE"
req GET  "/snapshot?node=bob"

echo
echo "================ causality scenario: interleaving with skew ================"
req POST /simulate samples/06-simulate-interleaving.json

echo
echo "================ causality scenario: timestamp order != causality ================"
req POST /simulate samples/07-simulate-concurrent.json

echo
echo "================ error: malformed JSON (expect HTTP 400) ================"
curl -s -o /tmp/hlc-err.json -w "HTTP %{http_code}\n" -X POST "$BASE/tick" \
  -H 'Content-Type: application/json' -d '{not json'
cat /tmp/hlc-err.json
echo
echo "[demo] done (server stopped by trap)"
