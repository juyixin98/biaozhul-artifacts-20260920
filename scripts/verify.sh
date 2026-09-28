#!/usr/bin/env bash
#
# One-shot, locally reproducible verification.
#
#   1. install (frozen lockfile if present)
#   2. typecheck
#   3. unit/integration/differential tests + coverage gate (>=80%)
#   4. build (tsc emit)
#   5. boot the REAL server against an isolated temp SQLite database
#   6. run the concrete end-to-end scenario driver over HTTP
#   7. tear the server down and report
#
# Everything is local: no external accounts or business data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8091}"
HOST="127.0.0.1"
BASE_URL="http://${HOST}:${PORT}"
WORKDIR="$(mktemp -d)"
DB_PATH="${WORKDIR}/service.sqlite"
LOG_PATH="${WORKDIR}/service.log"
SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

echo "== [1/6] dependency check =="
if [[ ! -d node_modules ]]; then
  npm ci 2>/dev/null || npm install
fi

echo "== [2/6] typecheck =="
npm run --silent typecheck

echo "== [3/6] tests + coverage (gate 80%) =="
npm run --silent coverage

echo "== [4/6] build =="
npm run --silent build

echo "== [5/6] boot server on ${BASE_URL} (isolated temp DB) =="
PORT="${PORT}" HOST="${HOST}" DATABASE_PATH="${DB_PATH}" LOG_FILE="${LOG_PATH}" \
  node dist/index.js >"${WORKDIR}/server.out" 2>&1 &
SERVER_PID=$!

# Wait for health endpoint.
for _ in $(seq 1 80); do
  if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "server exited early; output:" >&2
    cat "${WORKDIR}/server.out" >&2
    exit 1
  fi
  sleep 0.125
done
curl -fsS "${BASE_URL}/health" >/dev/null

echo "== [6/6] end-to-end concrete scenario =="
BASE_URL="${BASE_URL}" node scripts/e2e-scenario.mjs

echo
echo "== diagnostic log sample (request-correlated JSON lines) =="
grep -E 'e2e-(success|fail)-1' "${LOG_PATH}" | head -8 || true

echo
echo "ALL VERIFICATION STAGES PASSED"
