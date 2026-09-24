#!/usr/bin/env bash
# End-to-end demo for the DRF scheduler.
#
# Scenario: one cluster of 10 CPU / 10 memory units and two equal-weight
# tenants:
#   A = CPU-heavy, submits tasks needing 4 CPU, 0 mem
#   B = mem-heavy, submits tasks needing 0 CPU, 4 mem
# Tasks are indivisible; after a1,b1,a2,b2 are placed (8/8 used), a3 and b3
# cannot fit and stay queued. Releasing a1 and then b1 triggers rescheduling.
#
# Usage: ./demo.sh [base_url]   (default http://127.0.0.1:8080)
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"

req() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" -H 'Content-Type: application/json' -d "$body" "$BASE$path"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo
}

echo "== reset / configure: capacity 10 CPU, 10 mem =="
req POST /reset
req POST /config '{"cpu":10,"mem":10}'

echo "== tenants A (CPU-heavy) and B (mem-heavy), equal weight 1 =="
req POST /tenants '{"name":"A","weight":1}'
req POST /tenants '{"name":"B","weight":1}'

submit() { # id tenant cpu mem
  echo "-- submit $1: tenant=$2 demand={cpu:$3,mem:$4}"
  req POST /tasks "{\"id\":\"$1\",\"tenant\":\"$2\",\"demand\":{\"cpu\":$3,\"mem\":$4}}"
}

submit a1 A 4 0
submit b1 B 0 4
submit a2 A 4 0
submit b2 B 0 4
echo "== cluster now 8/8; next tasks cannot fit and must stay QUEUED =="
submit a3 A 4 0
submit b3 B 0 4

echo "== full state (conservation check) =="
req GET /state

echo "== release a1 (frees 4 CPU) -> a3 reschedules, b3 still queued =="
req DELETE /tasks/a1; echo

echo "== release b1 (frees 4 mem) -> b3 reschedules =="
req DELETE /tasks/b1; echo

echo "== final state =="
req GET /state
