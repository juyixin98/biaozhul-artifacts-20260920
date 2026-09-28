#!/usr/bin/env bash
# Reproduce every check and save reviewable artifacts under results/.
#
#   bash scripts/reproduce.sh
#
# Produces:
#   results/unit-<run-id>.log        - unit + integration test output
#   results/coverage-<run-id>.txt    - per-file coverage
#   results/live-<run-id>/           - live HTTP calls + stored evidence/log
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

RUN_ID="check-$(date -u +%Y%m%dT%H%M%SZ)"
OUT="results/${RUN_ID}"
mkdir -p "${OUT}"
echo "RUN_ID=${RUN_ID}" | tee "${OUT}/manifest.txt"
echo "python=$(python3 --version 2>&1)" | tee -a "${OUT}/manifest.txt"

echo "== installing/verifying locked dependencies =="
python3 -m pip install -q -r requirements.lock 2>/dev/null || \
  echo "(dependencies already present; continuing)" | tee -a "${OUT}/manifest.txt"

echo "== running test suite with coverage =="
set +e
python3 -m pytest -v --cov=strips_planner --cov-report=term-missing \
  > "${OUT}/tests.log" 2>&1
TEST_RC=$?
set -e
tail -n 40 "${OUT}/tests.log"
cp "${OUT}/tests.log" "${OUT}/../tests-latest.log"
if [ "${TEST_RC}" -ne 0 ]; then
  echo "tests failed (rc=${TEST_RC}); see ${OUT}/tests.log"
  exit "${TEST_RC}"
fi

echo "== live service run =="
export STRIPS_DB_PATH="${OUT}/evidence.db"
export STRIPS_LOG_PATH="${OUT}/runs.jsonl"
# Pick an ephemeral free port so the run never collides with other services.
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
echo "live port=${PORT}" | tee -a "${OUT}/manifest.txt"
PYTHONPATH=src python3 -m uvicorn strips_planner.main:app \
  --host 127.0.0.1 --port "${PORT}" >"${OUT}/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT
HEALTHY=0
for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 0.5
done
if [ "${HEALTHY}" -ne 1 ]; then
  echo "server failed to start; see ${OUT}/server.log" >&2
  cat "${OUT}/server.log" >&2
  exit 1
fi

STRIPS_BASE_URL="http://127.0.0.1:${PORT}" bash examples/curl_examples.sh \
  > "${OUT}/curl_examples.log" 2>&1
# Fail if any scenario returned a FastAPI default 404 (wrong route/server).
if grep -q '"detail":"Not Found"' "${OUT}/curl_examples.log"; then
  echo "live calls hit unmatched routes" >&2
  exit 1
fi

# Stop the server BEFORE copying the SQLite file so the WAL is checkpointed
# into evidence.db and the saved copy needs no -wal/-shm sidecars.
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true
trap - EXIT
cp "${STRIPS_DB_PATH}" "${OUT}/evidence.db.saved"
cp "${STRIPS_LOG_PATH}" "${OUT}/runs.jsonl.saved"
echo "artifacts written to ${OUT}" | tee -a "${OUT}/manifest.txt"
echo "DONE. inspect ${OUT}/"
