#!/usr/bin/env bash
# End-to-end demo for the consistent-hash rebalancing service.
# Requires: a running server (default http://127.0.0.1:8080) and curl + python3.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
j() { python3 -m json.tool; }

echo "### health"
curl -s "$BASE/health"; echo

echo "### 1. create old ring cluster-v1 (3 equal nodes)"
curl -s -X POST "$BASE/api/rings" -H 'Content-Type: application/json' -d '{
  "name": "cluster-v1",
  "vnodes_per_weight_unit": 64,
  "nodes": [
    {"id": "node-a", "weight": 1},
    {"id": "node-b", "weight": 1},
    {"id": "node-c", "weight": 1}
  ]
}' | j

echo "### 2. create new ring cluster-v2 (add node-d)"
curl -s -X POST "$BASE/api/rings" -H 'Content-Type: application/json' -d '{
  "name": "cluster-v2",
  "vnodes_per_weight_unit": 64,
  "nodes": [
    {"id": "node-a", "weight": 1},
    {"id": "node-b", "weight": 1},
    {"id": "node-c", "weight": 1},
    {"id": "node-d", "weight": 1}
  ]
}' | j

echo "### 3. list rings"
curl -s "$BASE/api/rings"; echo

echo "### 4. route keys (GET, repeatable ?key=)"
curl -s "$BASE/api/rings/cluster-v1/route?key=user:1001&key=user:1002" | j

echo "### 5. route keys (POST batch)"
curl -s -X POST "$BASE/api/rings/cluster-v2/route" \
  -H 'Content-Type: application/json' \
  -d '{"keys":["user:1001","user:1002","order:abc"]}' | j

echo "### 6. migration plan v1 -> v2 with probe keys"
curl -s -X POST "$BASE/api/plan" -H 'Content-Type: application/json' -d '{
  "old": "cluster-v1",
  "new": "cluster-v2",
  "probe_keys": ["user:1001", "user:1002", "order:abc"]
}' | j

echo "### 7. weight-change plan with inline rings (no stored state needed)"
curl -s -X POST "$BASE/api/plan" -H 'Content-Type: application/json' -d '{
  "old": {
    "vnodes_per_weight_unit": 64,
    "nodes": [{"id": "a", "weight": 1}, {"id": "b", "weight": 1}]
  },
  "new": {
    "vnodes_per_weight_unit": 64,
    "nodes": [{"id": "a", "weight": 3}, {"id": "b", "weight": 1}]
  }
}' | j
