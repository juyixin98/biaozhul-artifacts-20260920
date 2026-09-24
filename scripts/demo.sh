#!/usr/bin/env bash
# End-to-end acceptance demo against a locally running server.
# Usage:
#   scripts/demo.sh                     # starts the server on :8090 itself
# Requires: Go (CGO enabled), curl, python3 (for pretty-printing JSON).
set -euo pipefail

ADDR="${ADDR:-127.0.0.1:8090}"
BASE="http://${ADDR}"
WORKLOAD="demo-web"
SECRET="demo-secret-please-change"
DB="$(mktemp -u /tmp/scaler-demo-XXXX.db)"
BIN="$(mktemp -u /tmp/scaler-server-XXXX)"

trap 'kill "${SERVER_PID:-0}" 2>/dev/null || true; rm -f "$BIN" "$DB" "$DB-wal" "$DB-shm"' EXIT

j() { python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))'; }

echo "==> building server"
(cd "$(dirname "$0")/.." && go build -o "$BIN" ./cmd/server)

echo "==> starting server on ${ADDR} (db=${DB})"
SCALER_HMAC_SECRET="$SECRET" "$BIN" -addr ":${ADDR##*:}" -db "$DB" &
SERVER_PID=$!

for _ in $(seq 1 50); do
  curl -fsS "${BASE}/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

echo
echo "==> 1) configure: target 50%, tolerance 10%, stable window 60s, max 10"
curl -fsS -X PUT "${BASE}/api/v1/workloads/${WORKLOAD}/config" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/../examples/config.json" | j

echo
echo "==> 2) step UP: 4 pods @90% -> ratio 1.8 -> raw 8 (scale-up is immediate)"
curl -fsS -X POST "${BASE}/api/v1/workloads/${WORKLOAD}/decisions" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/../examples/decision-scaleup.json" | j

echo
echo "==> 3) step DOWN (t=30s): avg 18.75% -> raw 3, but window max is 8 -> hold"
curl -fsS -X POST "${BASE}/api/v1/workloads/${WORKLOAD}/decisions" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/../examples/decision-low-partial.json" | j

echo
echo "==> 4) out-of-order / stale event time must be rejected with 409"
curl -sS -o /tmp/scaler-stale.json -w 'HTTP %{http_code}\n' \
  -X POST "${BASE}/api/v1/workloads/${WORKLOAD}/decisions" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/../examples/decision-scaleup.json"
j < /tmp/scaler-stale.json
rm -f /tmp/scaler-stale.json

echo
echo "==> 5) decisions history"
curl -fsS "${BASE}/api/v1/workloads/${WORKLOAD}/decisions" | j

echo
echo "Demo finished. Server is shutting down."
