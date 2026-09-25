#!/usr/bin/env bash
# Request samples against a running tailsampler (default :8080).
# Usage: ./examples/requests.sh [base-url]
set -euo pipefail
BASE="${1:-http://localhost:8080}"
NOW_MS=$(($(date +%s%N) / 1000000))

echo "== health =="
curl -s "$BASE/healthz"; echo

echo "== ingest: one error trace (2 spans) =="
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' -d "{
  \"spans\": [
    {\"trace_id\":\"demo-err\",\"span_id\":\"r\",\"service\":\"checkout\",\"name\":\"POST /checkout\",\"start_unix_ms\":$NOW_MS,\"duration_ms\":40,\"status\":\"ok\"},
    {\"trace_id\":\"demo-err\",\"span_id\":\"c1\",\"parent_id\":\"r\",\"service\":\"payments\",\"name\":\"charge\",\"start_unix_ms\":$((NOW_MS+5)),\"duration_ms\":25,\"status\":\"error\"}
  ]}"; echo

echo "== ingest: one slow trace (kept by latency policy) =="
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' -d "{
  \"spans\": [
    {\"trace_id\":\"demo-slow\",\"span_id\":\"r\",\"service\":\"web\",\"name\":\"GET /report\",\"start_unix_ms\":$NOW_MS,\"duration_ms\":900,\"status\":\"ok\"}
  ]}"; echo

echo "== ingest: one fast healthy trace (will be dropped) =="
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' -d "{
  \"spans\": [
    {\"trace_id\":\"demo-ok\",\"span_id\":\"r\",\"service\":\"web\",\"name\":\"GET /health\",\"start_unix_ms\":$NOW_MS,\"duration_ms\":12,\"status\":\"ok\"}
  ]}"; echo

echo "== waiting for the decision window to close (default 10s)... =="
sleep 11

echo "== decisions =="
curl -s "$BASE/v1/decisions/demo-err"; echo
curl -s "$BASE/v1/decisions/demo-slow"; echo
curl -s "$BASE/v1/decisions/demo-ok"; echo

echo "== late error span for the already-dropped trace (decision must stay 'drop') =="
curl -s -X POST "$BASE/v1/spans" -H 'Content-Type: application/json' -d "{
  \"spans\": [
    {\"trace_id\":\"demo-ok\",\"span_id\":\"late-err\",\"parent_id\":\"r\",\"service\":\"web\",\"name\":\"late failure\",\"start_unix_ms\":$((NOW_MS+20)),\"duration_ms\":5,\"status\":\"error\"}
  ]}"; echo
curl -s "$BASE/v1/decisions/demo-ok"; echo

echo "== kept trace spans =="
curl -s "$BASE/v1/traces/demo-err"; echo

echo "== filtered lists =="
curl -s "$BASE/v1/decisions?keep=true"; echo
curl -s "$BASE/v1/decisions?incomplete=true"; echo

echo "== stats =="
curl -s "$BASE/v1/stats"; echo
