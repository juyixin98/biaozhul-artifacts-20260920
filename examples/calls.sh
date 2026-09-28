#!/usr/bin/env bash
# End-to-end call examples against a locally running server.
# Start it first:  make run   (or)  uvicorn min_iowl.api.app:app --reload
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
RID="demo-$(date +%s)"

echo "== health =="
curl -s "$BASE/health" | python3 -m json.tool

echo; echo "== create the unsatisfiable-but-consistent ontology (request id: $RID) =="
curl -s -X POST "$BASE/ontologies" \
  -H 'Content-Type: application/json' \
  -H "x-request-id: $RID" \
  --data @data/fixture_intersection_disjoint.json | python3 -m json.tool

echo; echo "== reason: Hermaphrodite unsatisfiable, ontology still consistent =="
curl -s -X POST "$BASE/ontologies/fixture-unsat-but-consistent/reason" \
  -H "x-request-id: $RID" | python3 -m json.tool

echo; echo "== explain one individual (bob) =="
curl -s -X POST "$BASE/ontologies/fixture-unsat-but-consistent/query?individual=bob" \
  | python3 -m json.tool

echo; echo "== correlated request log for $RID =="
curl -s "$BASE/requests/$RID" | python3 -m json.tool
