#!/usr/bin/env bash
# Minimal curl samples. Start the server first:
#   go run ./cmd/drfscheduler -capacity-cpu 4000 -capacity-mem 4000
set -u
B=http://127.0.0.1:8080

curl -s $B/healthz; echo
curl -s -X PUT $B/v1/tenants/A -d '{"weight":1}' -H 'Content-Type: application/json'; echo
curl -s -X PUT $B/v1/tenants/B -d '{"weight":1}' -H 'Content-Type: application/json'; echo

# big task occupies the whole 4000/4000 cluster for 100 virtual ms
curl -s -X POST $B/v1/tasks -d '{"id":"big","tenant_id":"A","cpu_millicpu":4000,"memory_mib":4000,"duration_ms":100}' \
  -H 'Content-Type: application/json'; echo
# small tasks must wait (state QUEUED) while big is non-preemptible
curl -s -X POST $B/v1/tasks -d '{"id":"s1","tenant_id":"B","cpu_millicpu":1000,"memory_mib":1000,"duration_ms":100}' \
  -H 'Content-Type: application/json'; echo
curl -s -X POST $B/v1/tasks -d '{"id":"s2","tenant_id":"B","cpu_millicpu":1000,"memory_mib":1000,"duration_ms":100}' \
  -H 'Content-Type: application/json'; echo

curl -s $B/v1/state; echo
# release: advance the fake clock past big; s1/s2 start
curl -s -X POST $B/v1/clock/advance -d '{"advance_ms":100}' -H 'Content-Type: application/json'; echo
# drain
curl -s -X POST $B/v1/clock/advance -d '{"advance_ms":500}' -H 'Content-Type: application/json'; echo
curl -s $B/v1/events; echo
