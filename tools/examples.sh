#!/usr/bin/env bash
# Fire every bundled sample request against a running server.
# Usage: tools/examples.sh [host:port]   (default localhost:8080)
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${1:-localhost:8080}"

say() { printf '\n========== %s ==========\n' "$1"; }

say "GET /health"
curl -sS "http://$BASE/health"; echo

say "POST /api/plan  (3-table chain, explicit selectivities)"
curl -sS -X POST "http://$BASE/api/plan" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/plan_basic.json; echo

say "POST /api/plan  (4-table star, NDV hints)"
curl -sS -X POST "http://$BASE/api/plan" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/plan_star_ndv.json; echo

say "POST /api/plan  (disconnected graph -> expect 422)"
curl -sS -X POST "http://$BASE/api/plan" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/plan_disconnected.json; echo

say "POST /api/plan  (same graph with allowCrossProducts=true)"
curl -sS -X POST "http://$BASE/api/plan" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/plan_cross.json; echo

say "POST /api/plan  (huge cardinalities -> cost overflow flag)"
curl -sS -X POST "http://$BASE/api/plan" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/plan_overflow.json; echo

say "POST /api/simulate (Zipf-skewed data: estimated vs true optimum)"
curl -sS -X POST "http://$BASE/api/simulate" \
  -H 'Content-Type: application/json' \
  --data-binary @samples/sim_skew.json; echo
