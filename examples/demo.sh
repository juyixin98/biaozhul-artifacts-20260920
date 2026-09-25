#!/usr/bin/env bash
# End-to-end demo against a fresh, temporary server instance.
# Usage: ./examples/demo.sh [PORT]
set -euo pipefail

PORT="${1:-18080}"
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
WAL="$(mktemp -d)/wal.jsonl"

echo ">> building"
( cd "$ROOT" && go build -o bin/histmerge ./cmd/histmerge )

echo ">> starting server on :${PORT} with WAL ${WAL}"
"$ROOT/bin/histmerge" -addr ":${PORT}" -wal "$WAL" &
SRV_PID=$!
trap 'kill $SRV_PID 2>/dev/null || true' EXIT
sleep 1

echo ">> health"
curl -s "${BASE}/healthz"; echo

echo ">> ingest two compatible-layout series"
curl -s -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  --data-binary "@${HERE}/ingest_compatible.json"; echo

echo ">> ingest one different-layout series (shares bounds 0.1,10 only)"
curl -s -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  --data-binary "@${HERE}/ingest_incompatible_layout.json"; echo

echo ">> ingest a valid empty histogram"
curl -s -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  --data-binary "@${HERE}/ingest_empty_histogram.json"; echo

echo ">> INVALID: non-monotonic buckets (expect rejected, HTTP 422)"
curl -s -o /tmp/hm-demo-err1.json -w "HTTP %{http_code}\n" -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  --data-binary "@${HERE}/ingest_bad_nonmonotonic.json"
cat /tmp/hm-demo-err1.json; echo

echo ">> INVALID: +Inf count != total_count (expect rejected, HTTP 422)"
curl -s -o /tmp/hm-demo-err2.json -w "HTTP %{http_code}\n" -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  --data-binary "@${HERE}/ingest_bad_inf_mismatch.json"
cat /tmp/hm-demo-err2.json; echo

echo ">> query: strict merge across incompatible layouts (expect HTTP 409)"
curl -s -o /tmp/hm-demo-strict.json -w "HTTP %{http_code}\n" \
  "${BASE}/api/v1/query?coarsen=false"
cat /tmp/hm-demo-strict.json; echo

echo ">> query: auto coarsening with quantile intervals"
curl -s -X POST "${BASE}/api/v1/query" \
  -H 'Content-Type: application/json' \
  --data-binary "@${HERE}/query_body.json"; echo

echo ">> query: same service, GET form, quantiles 0.5/0.99"
curl -s "${BASE}/api/v1/query?match%5B%5D=service%3Dcheckout&quantile=0.5&quantile=0.99"; echo

echo ">> series listing"
curl -s "${BASE}/api/v1/series"; echo

echo ">> shutting down (server stops; trap kills pid ${SRV_PID})"
