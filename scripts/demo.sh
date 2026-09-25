#!/usr/bin/env bash
# End-to-end smoke demo against a locally running server.
# Usage: ./scripts/demo.sh [base_url]   (default http://127.0.0.1:8080)
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if command -v jq >/dev/null 2>&1; then
  pp() { jq .; }
  runid() { jq -r .id; }
else
  pp() { python3 -m json.tool; }
  runid() { python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'; }
fi

echo "== health =="
curl -fsS "$BASE/health" | pp
echo

echo "== 1) diamond (all success) =="
OUT=$(curl -fsS -X POST "$BASE/runs" -H 'Content-Type: application/json' \
  -d @"$HERE/examples/diamond_success.json")
echo "$OUT" | pp
RID=$(echo "$OUT" | runid)
sleep 0.2
echo "run id: $RID"
curl -fsS "$BASE/runs/$RID" | pp
echo

echo "== 2) retry then succeed (flaky fails twice, 4-attempt budget) =="
RID2=$(curl -fsS -X POST "$BASE/runs" -H 'Content-Type: application/json' \
  -d @"$HERE/examples/retry_success.json" | runid)
sleep 0.7
curl -fsS "$BASE/runs/$RID2" | pp
echo "-- event log --"
curl -fsS "$BASE/runs/$RID2/events" | pp
echo

echo "== 3) failure propagation: skip vs all_finished =="
RID3=$(curl -fsS -X POST "$BASE/runs" -H 'Content-Type: application/json' \
  -d @"$HERE/examples/failure_skip.json" | runid)
sleep 0.5
curl -fsS "$BASE/runs/$RID3" | pp
echo

echo "== 4) cycle rejected at submit (expect HTTP 400) =="
curl -sS -o /tmp/dag_cycle_resp -w 'http_status=%{http_code}\n' \
  -X POST "$BASE/runs" -H 'Content-Type: application/json' \
  -d @"$HERE/examples/cycle_rejected.json" || true
cat /tmp/dag_cycle_resp; echo
echo

echo "== 5) cancellation =="
RID4=$(curl -fsS -X POST "$BASE/runs" -H 'Content-Type: application/json' \
  -d @"$HERE/examples/cancel_demo.json" | runid)
sleep 0.2
curl -fsS -X POST "$BASE/runs/$RID4/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"demo"}' | pp
sleep 0.2
curl -fsS "$BASE/runs/$RID4" | pp
echo

echo "== list all runs =="
curl -fsS "$BASE/runs" | pp
