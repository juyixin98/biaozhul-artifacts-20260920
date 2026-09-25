#!/usr/bin/env bash
# Request samples against a locally running service.
# Start first:  python3 -m sparse_retrieval.server --port 8080
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

echo "== health =="
curl -s "$BASE/health"; echo

echo "== add documents (doc 1 has a duplicate dim and a negative weight) =="
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"doc_id": 1, "vector": [[10, 1.0], [10, 0.5], [20, -0.75]]}'; echo
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"doc_id": 2, "vector": [[10, 1.5], [30, 0.25]]}'; echo
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"doc_id": 3, "vector": [[99, 1.0]]}'; echo
echo "== doc 4 is a zero vector (weights cancel): stored, cosine always 0 =="
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"doc_id": 4, "vector": [[5, 1.0], [5, -1.0]]}'; echo

echo "== query top-2 (note candidates_examined: docs 3 and 4 are pruned) =="
curl -s -X POST "$BASE/query" -H 'Content-Type: application/json' \
  -d '{"vector": [[10, 1.0], [20, 0.5]], "k": 2}'; echo

echo "== zero-vector query: everything scores 0.0, order by doc_id =="
curl -s -X POST "$BASE/query" -H 'Content-Type: application/json' \
  -d '{"vector": [], "k": 4}'; echo

echo "== stats =="
curl -s "$BASE/stats"; echo

echo "== delete doc 2 =="
curl -s -X DELETE "$BASE/documents/2"; echo

echo "== invalid request (negative dimension) -> 400 =="
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/documents" \
  -H 'Content-Type: application/json' -d '{"doc_id": 9, "vector": [[-1, 1.0]]}'
