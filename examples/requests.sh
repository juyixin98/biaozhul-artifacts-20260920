#!/usr/bin/env bash
# Seed a running server (localhost:8080 by default) with a small demo
# dataset through the HTTP API, then run example searches.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

post() { curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; echo; }

echo "== health =="
curl -sS "$BASE/health"; echo

echo "== batch insert (L2) =="
post /batch '{
  "metric": "L2",
  "vectors": [
    {"id": "a1", "vector": [0.1, 0.2, 0.3], "metadata": {"cat": "red"}},
    {"id": "a2", "vector": [0.2, 0.1, 0.4], "metadata": {"cat": "red"}},
    {"id": "a3", "vector": [5.0, 5.0, 5.0], "metadata": {"cat": "blue"}},
    {"id": "a4", "vector": [0.0, 0.0, 0.0], "metadata": {"cat": "blue"}}
  ]
}'

echo "== exact L2 search, top 3 =="
post /search '{"vector": [0.1, 0.2, 0.25], "metric": "L2", "k": 3, "mode": "exact"}'

echo "== filtered exact search (cat=red) =="
post /search '{"vector": [0.1, 0.2, 0.25], "metric": "L2", "k": 3,
               "mode": "exact", "filter": {"cat": "red"}}'

echo "== delete a1, then search again =="
post /delete '{"id": "a1"}'
post /search '{"vector": [0.1, 0.2, 0.25], "metric": "L2", "k": 3, "mode": "exact"}'

echo "== cosine batch + approximate search (reset first: fresh 3-D space) =="
post /reset '{}'
post /batch '{
  "metric": "COSINE",
  "vectors": [
    {"id": "d1", "vector": [1.0, 0.0, 0.0], "metadata": {"g": 1}},
    {"id": "d2", "vector": [0.9, 0.1, 0.0], "metadata": {"g": 1}},
    {"id": "d3", "vector": [0.0, 1.0, 0.0], "metadata": {"g": 2}}
  ]
}'
post /search '{"vector": [1.0, 0.0, 0.0], "metric": "COSINE", "k": 2,
               "mode": "approx", "nprobe": 8}'

echo "== zero vector rejected under COSINE =="
post /insert '{"id": "zero", "vector": [0.0, 0.0, 0.0], "metric": "COSINE"}'

echo "== stats =="
curl -sS "$BASE/stats"; echo
