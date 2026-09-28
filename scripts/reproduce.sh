#!/usr/bin/env bash
# Full offline reproduction. Produces reviewable artefacts under results/:
#   results/test-output.txt      - full pytest run
#   results/coverage.txt         - coverage report (80% gate)
#   results/cli-*.txt            - CLI reasoning per fixture (kernel + oracle)
#   results/http-*.json          - real server responses, normal and abnormal
#   results/server.log           - correlated structured server log
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH=src
export MINIOWL_DB="$PWD/results/repro.sqlite3"
mkdir -p results
PORT=8011
BASE="http://127.0.0.1:${PORT}"

echo "[1/5] test suite"
python3 -m pytest -q 2>&1 | tee results/test-output.txt

echo "[2/5] coverage gate"
python3 -m pytest --cov=src/min_iowl --cov-report=term-missing --cov-fail-under=80 -q \
  2>&1 | tee results/coverage.txt

echo "[3/5] CLI reasoning over fixtures"
for f in data/fixture_hierarchy.json data/fixture_intersection_disjoint.json data/fixture_mutex_instance.json; do
  name="$(basename "$f" .json)"
  python3 scripts/reason.py "$f" | tee "results/cli-${name}.txt"
  python3 scripts/reason.py "$f" --json > "results/cli-${name}.json"
done
python3 scripts/reason.py --functional data/fixture_hierarchy.owlf | tee results/cli-functional.txt

echo "[4/5] start server on ${BASE}"
rm -f "$MINIOWL_DB"
uvicorn min_iowl.api.app:app --host 127.0.0.1 --port "$PORT" >results/server.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done

echo "  [normal] create + reason, all three fixtures"
curl -s "$BASE/health" | tee results/http-health.json; echo
curl -s -X POST "$BASE/ontologies" -H 'Content-Type: application/json' \
  -H 'x-request-id: repro-hierarchy' --data @data/fixture_hierarchy.json \
  | tee results/http-create-hierarchy.json; echo
curl -s -X POST "$BASE/ontologies/fixture-hierarchy/reason" -H 'x-request-id: repro-hierarchy' \
  | tee results/http-reason-hierarchy.json; echo
curl -s -X POST "$BASE/ontologies" -H 'Content-Type: application/json' \
  -H 'x-request-id: repro-unsat' --data @data/fixture_intersection_disjoint.json >/dev/null
curl -s -X POST "$BASE/ontologies/fixture-unsat-but-consistent/reason" -H 'x-request-id: repro-unsat' \
  | tee results/http-reason-unsat-consistent.json; echo
curl -s -X POST "$BASE/ontologies" -H 'Content-Type: application/json' \
  -H 'x-request-id: repro-mutex' --data @data/fixture_mutex_instance.json >/dev/null
curl -s -X POST "$BASE/ontologies/fixture-mutex-instance/reason" -H 'x-request-id: repro-mutex' \
  | tee results/http-reason-mutex.json; echo
curl -s -X POST "$BASE/ontologies/fixture-mutex-instance/query?individual=mallory" \
  | tee results/http-query-mallory.json; echo

echo "  [abnormal] unsupported construct, unknown ontology/individual, duplicate id"
curl -s -o results/http-err-unsupported.json -w 'status=%{http_code}\n' \
  -X POST "$BASE/ontologies" -H 'Content-Type: application/json' \
  --data @data/fixture_unsupported_rejected.json | tee results/http-err-unsupported.status
cat results/http-err-unsupported.json; echo
curl -s -o results/http-err-unknown-onto.json -w 'status=%{http_code}\n' \
  -X POST "$BASE/ontologies/does-not-exist/reason" | tee results/http-err-unknown-onto.status
cat results/http-err-unknown-onto.json; echo
curl -s -o results/http-err-unknown-individual.json -w 'status=%{http_code}\n' \
  -X POST "$BASE/ontologies/fixture-hierarchy/query?individual=ghost" \
  | tee results/http-err-unknown-individual.status
cat results/http-err-unknown-individual.json; echo
curl -s -o /dev/null -w 'duplicate-create status=%{http_code}\n' \
  -X POST "$BASE/ontologies" -H 'Content-Type: application/json' \
  --data @data/fixture_hierarchy.json
curl -s "$BASE/requests/repro-mutex" | tee results/http-request-trace.json; echo

echo "[5/5] stop server"
kill "$SRV" 2>/dev/null || true
trap - EXIT
echo "Done. Artefacts in results/ (server log: results/server.log)"
