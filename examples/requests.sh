#!/usr/bin/env bash
# Request samples against a locally running server (default 127.0.0.1:8080).
# Usage: ./examples/requests.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:8080}"

echo "== 1. tenant-a submits a CPU compute job =="
JOB_A=$(curl -s -X POST "$BASE/v1/compute" \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"report","payload":"quarterly-numbers","iterations":1000,"mem_bytes":4096}')
echo "$JOB_A"
JOB_A_ID=$(echo "$JOB_A" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')

echo "== 2. tenant-b submits a job under the SAME key (isolated) =="
curl -s -X POST "$BASE/v1/compute" \
  -H 'X-Tenant-ID: tenant-b' -H 'Content-Type: application/json' \
  -d '{"key":"report","payload":"other-numbers","iterations":1000,"mem_bytes":4096}'
echo

echo "== 3. attempt to override tenant via body (rejected 400) =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/compute" \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"tenant_id":"tenant-b","key":"x","payload":"p","iterations":1,"mem_bytes":1}'

echo "== 4. missing tenant header (rejected 401) =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' \
  -d '{"key":"x","payload":"p","iterations":1,"mem_bytes":1}'

sleep 1

echo "== 5. each tenant reads its own cache entry for key 'report' =="
echo -n "tenant-a: "; curl -s -H 'X-Tenant-ID: tenant-a' "$BASE/v1/cache/report" | xxd -p; echo
echo -n "tenant-b: "; curl -s -H 'X-Tenant-ID: tenant-b' "$BASE/v1/cache/report" | xxd -p; echo

echo "== 6. cross-tenant job access (404) =="
curl -s -w '\nHTTP %{http_code}\n' -H 'X-Tenant-ID: tenant-b' "$BASE/v1/jobs/$JOB_A_ID"

echo "== 7. fault injection: fail the next compute call =="
go run ./cmd/faultclient -addr "$BASE" -fail-next 1
JOB_F=$(curl -s -X POST "$BASE/v1/compute" \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"doomed","payload":"p","iterations":1,"mem_bytes":1}')
JOB_F_ID=$(echo "$JOB_F" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
sleep 1
curl -s -H 'X-Tenant-ID: tenant-a' "$BASE/v1/jobs/$JOB_F_ID"; echo

echo "== 8. cancellation =="
go run ./cmd/faultclient -addr "$BASE" -latency 5s
curl -s -X POST "$BASE/v1/compute" -H 'X-Tenant-ID: tenant-a' \
  -d '{"key":"slow1","payload":"p","iterations":1,"mem_bytes":1}' > /dev/null
JOB_C=$(curl -s -X POST "$BASE/v1/compute" -H 'X-Tenant-ID: tenant-a' \
  -d '{"key":"slow2","payload":"p","iterations":1,"mem_bytes":1}')
JOB_C_ID=$(echo "$JOB_C" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
curl -s -X DELETE -H 'X-Tenant-ID: tenant-a' "$BASE/v1/jobs/$JOB_C_ID"; echo
go run ./cmd/faultclient -addr "$BASE" -latency 0s
# Cancel of a running job is asynchronous; poll for the terminal state.
for i in $(seq 1 20); do
  ST=$(curl -s -H 'X-Tenant-ID: tenant-a' "$BASE/v1/jobs/$JOB_C_ID" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
  [ "$ST" = "canceled" ] && break
  sleep 0.2
done
echo "final status of $JOB_C_ID: $ST"

echo "== 9. job status =="
curl -s -H 'X-Tenant-ID: tenant-a' "$BASE/v1/jobs/$JOB_A_ID"; echo
