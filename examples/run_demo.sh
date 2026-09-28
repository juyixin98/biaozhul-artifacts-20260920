#!/usr/bin/env bash
# End-to-end demo: start the service, run every fixture through the real HTTP
# API (success AND failure paths), and save the responses for review.
#
# Usage:  bash examples/run_demo.sh
# Output: results/<fixture>.response.json + results/demo.log
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
PORT=8419
BASE="http://127.0.0.1:${PORT}"
mkdir -p results

export OWL_DATA_DIR="$(pwd)/results/runtime"
export OWL_LOG_DIR="$(pwd)/results/runtime"
rm -rf results/runtime

echo "== starting service on ${BASE} (log: results/runtime/service.log)"
OWL_LOG_LEVEL=INFO ${PY} -m uvicorn app.main:create_app \
    --factory --host 127.0.0.1 --port ${PORT} \
    > results/runtime-server.log 2>&1 &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

for i in $(seq 1 50); do
    curl -sf "${BASE}/health" > /dev/null && break
    sleep 0.2
done
echo "== health:"
curl -s "${BASE}/health" | ${PY} -m json.tool | tee results/health.json

for fx in multi_inheritance equivalence_ring intersection_disjoint_conflict \
          two_assertions_conflict unsupported_union; do
    echo "== POST /reason  fixture=${fx}"
    curl -s -X POST "${BASE}/reason" \
        -H 'Content-Type: application/json' \
        -H "X-Request-Id: demo-${fx}" \
        -d @"fixtures/${fx}.json" \
        | ${PY} -m json.tool > "results/${fx}.response.json" || true
    head -c 400 "results/${fx}.response.json"; echo " ..."
done

echo "== POST /subclass (EmperorPenguin <= Swimmer)"
curl -s -X POST "${BASE}/subclass" \
    -H 'Content-Type: application/json' \
    -d '{"sub":"EmperorPenguin","super":"Swimmer",
         "axioms":[{"sub":"EmperorPenguin","super":"Penguin"},
                   {"sub":"Penguin","super":"Swimmer"}]}' \
    | ${PY} -m json.tool | tee results/subclass.response.json

echo "== GET /requests/demo-equivalence_ring (audit trail)"
curl -s "${BASE}/requests/demo-equivalence_ring" \
    | ${PY} -m json.tool > results/audit_trail.json || true
head -c 400 results/audit_trail.json; echo " ..."

echo "== service log (key steps with request ids):"
grep -E "req_|demo-" results/runtime/service.log | head -20 || true

echo "== done. Responses saved under results/"
