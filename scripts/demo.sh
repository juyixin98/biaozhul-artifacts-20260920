#!/usr/bin/env bash
# End-to-end demo against a sim-mode (fake clock) DRF scheduler.
# Usage: scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== health =="
curl -s "$BASE/healthz" | j

echo "== set cluster capacity: 9000 millicpu, 18000 MiB =="
curl -s -X PUT "$BASE/v1/cluster/capacity" \
  -H 'Content-Type: application/json' \
  -d '{"cpu_millicpu":9000,"memory_mib":18000}' | j

echo "== tenants A (weight 1) and B (weight 1) =="
curl -s -X PUT "$BASE/v1/tenants/A" -H 'Content-Type: application/json' -d '{"weight":1}' | j
curl -s -X PUT "$BASE/v1/tenants/B" -H 'Content-Type: application/json' -d '{"weight":1}' | j

echo "== submit the classic DRF workload as ONE batch (same virtual instant) =="
# A tasks: <1 CPU, 4 GB> (100ms); B1/B3 <3 CPU, 1 GB> (200ms); B2 (50ms) is
# the first release. Submitting as a batch reproduces the hand-computed order
# A1 B1 A2 B2 A3.
curl -s -X POST "$BASE/v1/tasks/batch" -H 'Content-Type: application/json' -d '[
  {"id":"A1","tenant_id":"A","cpu_millicpu":1000,"memory_mib":4000,"duration_ms":100},
  {"id":"A2","tenant_id":"A","cpu_millicpu":1000,"memory_mib":4000,"duration_ms":100},
  {"id":"A3","tenant_id":"A","cpu_millicpu":1000,"memory_mib":4000,"duration_ms":100},
  {"id":"A4","tenant_id":"A","cpu_millicpu":1000,"memory_mib":4000,"duration_ms":100},
  {"id":"B1","tenant_id":"B","cpu_millicpu":3000,"memory_mib":1000,"duration_ms":200},
  {"id":"B2","tenant_id":"B","cpu_millicpu":3000,"memory_mib":1000,"duration_ms":50},
  {"id":"B3","tenant_id":"B","cpu_millicpu":3000,"memory_mib":1000,"duration_ms":200}
]' | j

echo "== state at t=0: expect A1,B1,A2,B2,A3 RUNNING; A4,B3 QUEUED =="
curl -s "$BASE/v1/state" | j

echo "== advance virtual clock 50ms (B2 finishes; B3 starts) =="
curl -s -X POST "$BASE/v1/clock/advance" -H 'Content-Type: application/json' \
  -d '{"advance_ms":50}' | j

echo "== advance another 50ms (A1,A2,A3 finish; A4 starts) =="
curl -s -X POST "$BASE/v1/clock/advance" -H 'Content-Type: application/json' \
  -d '{"advance_ms":50}' | j

echo "== advance 500ms: everything drains =="
curl -s -X POST "$BASE/v1/clock/advance" -H 'Content-Type: application/json' \
  -d '{"advance_ms":500}' | j

echo "== structured event log (start/finish order) =="
curl -s "$BASE/v1/events" | j
