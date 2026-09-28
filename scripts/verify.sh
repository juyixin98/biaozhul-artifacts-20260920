#!/usr/bin/env bash
#
# Full verification gate for the RFC 6902 patch service.
#
#   1. install is assumed done (npm ci / npm install)
#   2. typecheck + build
#   3. regenerate independent-oracle fixtures (Python stdlib) and fail if the
#      committed fixtures drift from the generator
#   4. unit + independent cross-check test suites
#   5. boot the real server on an ephemeral port with a temp SQLite file
#   6. run concrete-result HTTP smoke checks against it
#
# Checks that cannot run in this environment are listed explicitly as
# NOT-EXECUTED at the end, never reported as passing.

set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-3109}"
DB_FILE="$(mktemp -d)/verify.sqlite"
BASE="http://127.0.0.1:${PORT}"
SERVER_PID=""

section() { printf '\n=== %s ===\n' "$1"; }

cleanup() {
  if [[ -n "${SERVER_PID}" ]]; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

section "1/6 typecheck"
npm run --silent typecheck

section "2/6 build"
npm run --silent build

section "3/6 regenerate independent oracle fixtures and check for drift"
cp test/fixtures/crosscheck-generated.json /tmp/crosscheck-before.json
python3 scripts/generate-crosscheck.py 6902 140
if ! diff -u /tmp/crosscheck-before.json test/fixtures/crosscheck-generated.json; then
  echo "FAIL: committed crosscheck fixtures are not reproducible from the independent generator"
  exit 1
fi
echo "fixtures reproducible: committed file matches generator output"

section "4/6 unit + independent cross-check tests"
node --no-warnings=ExperimentalWarning --import tsx --test \
  test/pointer.test.ts \
  test/contract.test.ts \
  test/kernel.test.ts \
  test/crosscheck.test.ts \
  test/store.test.ts \
  test/api.test.ts

section "5/6 boot real server (port ${PORT}, temp db ${DB_FILE})"
DATABASE_PATH="${DB_FILE}" PORT="${PORT}" HOST="127.0.0.1" \
  node --no-warnings=ExperimentalWarning dist/server.js >/tmp/verify-server.log 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  if curl -sf "${BASE}/health" >/dev/null 2>&1; then
    echo "server is up"; break
  fi
  sleep 0.2
done
if ! curl -sf "${BASE}/health" >/dev/null 2>&1; then
  echo "server failed to start; log:"; cat /tmp/verify-server.log; exit 1
fi

section "6/6 HTTP smoke checks (concrete results + failure categories)"
node scripts/http-smoke.mjs "${BASE}"

section "server-side structured log sample"
grep -E 'requestId|server.listening' /tmp/verify-server.log | tail -8 || true

cat <<'NOTE'

=== NOT EXECUTED IN THIS ENVIRONMENT (explicitly listed, not claimed as passing) ===
- Real network TLS / reverse-proxy termination (service speaks plain HTTP on loopback).
- Multi-process write contention under SQLite (verified only within one process/connection).
- Load/performance testing and authenticated multi-tenant authorization (out of scope
  for this local, account-free service).
NOTE
