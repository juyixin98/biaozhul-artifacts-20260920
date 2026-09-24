#!/usr/bin/env bash
# Fire every example request at a locally running server.
# Usage: scripts/run_examples.sh [base_url]   (default http://127.0.0.1:8000)
set -u
BASE="${1:-http://127.0.0.1:8000}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/examples/requests"

for f in "$DIR"/*.json; do
  echo "=============================================================="
  echo "POST $BASE/v1/verify  <-  $(basename "$f")"
  echo "--------------------------------------------------------------"
  curl -sS -X POST "$BASE/v1/verify" \
    -H "Content-Type: application/json" \
    --data-binary @"$f" \
    | python3 -m json.tool || echo "(request failed; is the server up?)"
  echo
done
