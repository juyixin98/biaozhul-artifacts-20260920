#!/usr/bin/env bash
# Build, (re)start the server with demo-friendly timing, then run the
# end-to-end acceptance script.
#
# Requires the database/role from scripts/db-setup.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

export AGING_STEP_MS="${AGING_STEP_MS:-100}"
export SWEEP_INTERVAL="${SWEEP_INTERVAL:-500ms}"
export HTTP_ADDR="${HTTP_ADDR:-:8068}"

echo "==> building"
go build -o bin/deadlock-server ./cmd/deadlock-server

echo "==> stopping an old server (if any)"
pkill -x deadlock-server 2>/dev/null || true
sleep 0.4

echo "==> starting server (AGING_STEP_MS=$AGING_STEP_MS SWEEP_INTERVAL=$SWEEP_INTERVAL)"
nohup ./bin/deadlock-server > /tmp/deadlock-server.log 2>&1 &
SRV=$!
trap 'echo "(demo server pid $SRV left running; stop with: kill $SRV)"' EXIT

for _ in $(seq 1 50); do
  curl -sf http://localhost:8068/healthz >/dev/null && break
  sleep 0.1
done

echo "==> running acceptance script"
scripts/acceptance.sh
