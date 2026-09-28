#!/usr/bin/env bash
# Example curl calls against a locally running server:
#   uvicorn hvp_service.main:app --port 8000
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

# 1. Register f(x) = sum(sin(x) * x) for x in R^2.
GRAPH_ID=$(curl -sS -X POST "$BASE/graphs" -H 'Content-Type: application/json' -d '{
  "nodes": [
    {"op": "input", "params": {"name": "x", "shape": [2]}},
    {"op": "sin", "inputs": [0]},
    {"op": "mul", "inputs": [1, 0]},
    {"op": "sum", "inputs": [2]}
  ],
  "output": 3
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["graph_id"])')
echo "graph_id=$GRAPH_ID"

# 2. HVP at x=[0.3, -0.7], direction v=[1, 1].
curl -sS -X POST "$BASE/graphs/$GRAPH_ID/hvp" -H 'Content-Type: application/json' -d '{
  "point": [0.3, -0.7],
  "vector": [1.0, 1.0]
}' | python3 -m json.tool

# 3. Non-smooth graph f(x) = sum(abs(x)) at a kink: rejected by default.
ABS_ID=$(curl -sS -X POST "$BASE/graphs" -H 'Content-Type: application/json' -d '{
  "nodes": [
    {"op": "input", "params": {"name": "x", "shape": [2]}},
    {"op": "abs", "inputs": [0]},
    {"op": "sum", "inputs": [1]}
  ],
  "output": 2
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["graph_id"])')

echo "--- reject policy (default) ---"
curl -sS -X POST "$BASE/graphs/$ABS_ID/hvp" -H 'Content-Type: application/json' -d '{
  "point": [0.0, 2.0],
  "vector": [1.0, 1.0]
}' | python3 -m json.tool

echo "--- subgradient policy with g0 = 0.5 ---"
curl -sS -X POST "$BASE/graphs/$ABS_ID/hvp" -H 'Content-Type: application/json' -d '{
  "point": [0.0, 2.0],
  "vector": [1.0, 1.0],
  "nonsmooth_policy": "subgradient",
  "subgradient": 0.5
}' | python3 -m json.tool

# 4. Cleanup.
curl -sS -X DELETE "$BASE/graphs/$GRAPH_ID" -o /dev/null -w 'deleted graph: %{http_code}\n'
curl -sS -X DELETE "$BASE/graphs/$ABS_ID" -o /dev/null -w 'deleted abs graph: %{http_code}\n'
