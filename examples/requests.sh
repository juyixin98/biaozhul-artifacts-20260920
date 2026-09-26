#!/usr/bin/env bash
# Request samples for the multi-tenant compute service.
# Usage: ./examples/requests.sh [host:port]
set -euo pipefail

ADDR="${1:-127.0.0.1:8080}"
BASE="http://$ADDR"

echo '--- health ---'
curl -s "$BASE/healthz"; echo

echo '--- tenant-a computes key "shared-key" (work=100000), waiting for result ---'
curl -s -X POST "$BASE/v1/compute?wait=true" \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"shared-key","work":100000,"mem_bytes":4096}'; echo

echo '--- tenant-b computes the SAME key "shared-key" (work=200000) ---'
curl -s -X POST "$BASE/v1/compute?wait=true" \
  -H 'X-Tenant-ID: tenant-b' -H 'Content-Type: application/json' \
  -d '{"key":"shared-key","work":200000,"mem_bytes":4096}'; echo

echo '--- tenant-a reads the shared cache key (sees only its own value) ---'
curl -s "$BASE/v1/cache/shared-key" -H 'X-Tenant-ID: tenant-a'; echo

echo '--- tenant-b reads the shared cache key (sees only its own value) ---'
curl -s "$BASE/v1/cache/shared-key" -H 'X-Tenant-ID: tenant-b'; echo

echo '--- async submit + poll + cancel ---'
JOB=$(curl -s -X POST "$BASE/v1/compute" \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"long-job","work":900000000,"mem_bytes":1048576}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
echo "submitted $JOB"
curl -s "$BASE/v1/jobs/$JOB" -H 'X-Tenant-ID: tenant-a'; echo
curl -s -X DELETE "$BASE/v1/jobs/$JOB" -H 'X-Tenant-ID: tenant-a'; echo
curl -s "$BASE/v1/jobs/$JOB" -H 'X-Tenant-ID: tenant-a'; echo

echo '--- tenant override attempt in body (rejected, HTTP 400) ---'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/compute" \
  -H 'X-Tenant-ID: tenant-a' -H 'Content-Type: application/json' \
  -d '{"key":"k","work":1000,"tenant_id":"tenant-b"}'

echo '--- missing auth header (rejected, HTTP 401) ---'
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' -d '{"key":"k","work":1000}'

echo '--- per-tenant stats (each tenant sees only its own row) ---'
curl -s "$BASE/v1/stats" -H 'X-Tenant-ID: tenant-a'; echo
