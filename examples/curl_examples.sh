#!/usr/bin/env bash
# Ready-to-run example requests against a local server (./run.sh first).
# All numbers are exact scalars: ints, decimal strings or "p/q" ratio strings.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== health =="
curl -s "$BASE/health" | python3 -m json.tool

echo "== unique solution with a big common factor =="
curl -s -X POST "$BASE/solve" -H 'content-type: application/json' -d '{
  "A": [[12, 18], [20, -10]],
  "b": [42, 30]
}' | python3 -m json.tool

echo "== rank-deficient, infinitely many solutions (parametric) =="
curl -s -X POST "$BASE/solve" -H 'content-type: application/json' -d '{
  "A": [[1, 1, 1], [2, 2, 2], [1, 0, 1]],
  "b": [1, 2, 1]
}' | python3 -m json.tool

echo "== inconsistent system (left-null-space witness y: y^T A=0, y^T b!=0) =="
curl -s -X POST "$BASE/solve" -H 'content-type: application/json' -d '{
  "A": [[1, 1, 1], [1, 1, 1], [2, -1, 1]],
  "b": [1, 3, 0]
}' | python3 -m json.tool

echo "== exact where float64 cannot tell the matrix from a singular one =="
curl -s -X POST "$BASE/solve" -H 'content-type: application/json' -d '{
  "A": [[1, 2, 3], [4, 5, 6], [7, 8, "9000000000000001/1000000000000000"]],
  "b": [6, 15, "24000000000000001/1000000000000000"]
}' | python3 -m json.tool

echo "== float literal is refused (distinct error class, never silently exact) =="
curl -s -X POST "$BASE/solve" -H 'content-type: application/json' -d '{
  "A": [[1, 0.5]], "b": [1]
}' | python3 -m json.tool

echo "== digit budget exhausted -> 422 with diagnosable progress =="
curl -s -X POST "$BASE/solve" -H 'content-type: application/json' -d '{
  "A": [[1,2,3,4,5],[2,3,4,5,6],[3,4,5,6,7],[4,5,6,7,8],[5,6,7,8,10]],
  "b": [1,2,3,4,5],
  "digit_budget": 1
}' | python3 -m json.tool
