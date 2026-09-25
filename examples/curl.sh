#!/usr/bin/env bash
# Request samples for the tail-sampling decision backend.
# Usage: start the server first (see README), then: bash examples/curl.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

echo "### health"
curl -s "$BASE/healthz" | jq .

echo "### ingest an error trace"
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  -d @examples/spans_error.json | jq .

echo "### ingest a slow (tail-latency) trace"
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  -d @examples/spans_slow.json | jq .

echo "### ingest a fast trace (default drop)"
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' \
  -d @examples/spans_fast.json | jq .

echo "### wait for the decision wait window, then query each trace"
sleep 2.2
curl -s "$BASE/v1/decisions/trace-err-001" | jq .
curl -s "$BASE/v1/decisions/trace-slow-001" | jq .
curl -s "$BASE/v1/decisions/trace-fast-001" | jq .

echo "### a late error span after finalization (decision stays immutable)"
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' -d '{
  "spans": [{
    "trace_id": "trace-fast-001", "span_id": "late-err",
    "parent_span_id": "root", "name": "late async failure",
    "status": "ERROR", "error_event": "queue nack",
    "start_time_ms": 1700000002100, "duration_ms": 2
  }]
}' | jq .
curl -s "$BASE/v1/decisions/trace-fast-001" | jq '{kept, reason_code, late_spans}'

echo "### list decisions (newest first), filter kept=true"
curl -s "$BASE/v1/decisions?kept=true" | jq '.decisions[] | {trace_id, kept, policy}'

echo "### stats (counters + budget snapshot)"
curl -s "$BASE/v1/stats" | jq .

echo "### force-finalize open traces (incomplete ones get FORCED_INCOMPLETE)"
curl -s -X POST "$BASE/admin/flush" | jq '.finalized'
