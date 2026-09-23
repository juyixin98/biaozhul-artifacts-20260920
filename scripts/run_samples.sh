#!/usr/bin/env bash
# Runs every saved sample against a live server and stores responses in samples/results/.
# Usage: scripts/run_samples.sh [base_url]   (default http://localhost:8080)
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-http://localhost:8080}"
mkdir -p samples/results

call() {
  local endpoint="$1" file="$2" outfile="$3"
  echo ">> POST $endpoint  ($file)"
  code=$(curl -sS -o "samples/results/$outfile" -w "%{http_code}" \
    -H 'Content-Type: application/json' \
    -X POST --data-binary "@samples/$file" "$BASE$endpoint")
  echo "   HTTP $code -> samples/results/$outfile"
}

curl -sS "$BASE/health" -o samples/results/health.json -w '>> GET /health -> HTTP %{http_code}\n'
call /plan      plan_tpch_like.json      plan_tpch_like.json
call /plan      plan_chain3.json         plan_chain3.json
call /plan      plan_missing_stats.json  plan_missing_stats.json
call /plan      plan_overflow.json       plan_overflow.json
call /plan      plan_disconnected.json   plan_disconnected.json
call /enumerate enumerate_k4.json        enumerate_k4.json
call /simulate  simulate_uniform.json    simulate_uniform.json
call /simulate  simulate_skew.json       simulate_skew.json
echo "Done. Responses in samples/results/."
