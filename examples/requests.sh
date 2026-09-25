#!/usr/bin/env bash
# End-to-end smoke script against a locally running fairdrf server.
# Usage:
#   go run ./cmd/fairdrf -addr :8080 -cpu-milli 10000 -mem-mib 10240
#   ./examples/requests.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"

j() { python3 -m json.tool; }

echo "== health =="
curl -sS "$BASE/healthz" | j

echo "== create tenants (weights 1 and 2) =="
curl -sS -X POST "$BASE/tenants" -H 'Content-Type: application/json' \
  -d '{"id":"team-batch","weight":1}' | j
curl -sS -X POST "$BASE/tenants" -H 'Content-Type: application/json' \
  -d '{"id":"team-analytics","weight":2}' | j

echo "== submit a big task for team-batch (6 CPU / 6 GiB, 5s) =="
curl -sS -X POST "$BASE/tasks" -H 'Content-Type: application/json' -d '{
  "id":"batch-1","tenant_id":"team-batch",
  "request":{"cpu_milli":6000,"mem_mib":6144},"duration":"5s"
}' | j

echo "== submit two small analytics tasks =="
curl -sS -X POST "$BASE/tasks" -H 'Content-Type: application/json' -d '{
  "id":"ana-1","tenant_id":"team-analytics",
  "request":{"cpu_milli":1000,"mem_mib":1024},"duration":"8s"
}' | j
curl -sS -X POST "$BASE/tasks" -H 'Content-Type: application/json' -d '{
  "id":"ana-2","tenant_id":"team-analytics",
  "request":{"cpu_milli":1000,"mem_mib":1024},"duration":"8s"
}' | j

echo "== submit an infeasible task (exceeds total capacity) =="
curl -sS -X POST "$BASE/tasks" -H 'Content-Type: application/json' -d '{
  "id":"batch-huge","tenant_id":"team-batch",
  "request":{"cpu_milli":99999,"mem_mib":99999},"duration":"10s"
}' | j

echo "== cluster snapshot =="
curl -sS "$BASE/snapshot" | j

echo "== event log =="
curl -sS "$BASE/events" | j

echo
echo "Wait ~10s then re-check the snapshot to see releases/admissions:"
echo "  curl -sS $BASE/snapshot | python3 -m json.tool"
