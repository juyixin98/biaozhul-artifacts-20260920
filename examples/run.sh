#!/usr/bin/env bash
# End-to-end smoke run against a locally started contractcheckd.
# Usage: ./examples/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

go build -o /tmp/contractcheckd ./cmd/contractcheckd
/tmp/contractcheckd -addr 127.0.0.1:28080 -registry-addr 127.0.0.1:29001 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 0.5
BASE=http://127.0.0.1:28080

echo '=== 1. health ==='
curl -s $BASE/healthz; echo

echo '=== 2. registry-backed check (orders v1 -> v2, incompatible both directions) ==='
curl -s -X POST $BASE/v1/compatibility/check \
  -H 'Content-Type: application/json' \
  -d @examples/check-orders.json; echo

echo '=== 3. inline check (same pair, no registry) ==='
curl -s -X POST $BASE/v1/compatibility/checkInline \
  -H 'Content-Type: application/json' \
  -d @examples/check-inline.json; echo

echo '=== 4. unsupported keywords -> unknown ==='
curl -s -X POST $BASE/v1/compatibility/checkInline \
  -H 'Content-Type: application/json' \
  -d @examples/check-inline-unknown.json; echo

echo '=== 5. fault injection via headers (registry always fails) ==='
curl -s -X POST $BASE/v1/compatibility/check \
  -H 'Content-Type: application/json' \
  -H 'X-Fault-Error-Rate: 1' \
  -H 'X-Fault-Latency-Ms: 50' \
  -d @examples/check-orders.json; echo
