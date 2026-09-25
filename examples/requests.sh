#!/usr/bin/env bash
# Request walkthrough for the model-switch service.
#
# Prereqs (in two terminals from the repo root):
#   python3 scripts/seed_artifacts.py --root ./artifacts
#   python3 scripts/serve.py --root ./artifacts --initial v1 --port 8000
#
# Then:  bash examples/requests.sh
set -u
BASE="${BASE:-http://127.0.0.1:8000}"

j() { python3 -m json.tool 2>/dev/null || cat; }   # pretty-print when possible

echo "### 0. liveness";
curl -s "$BASE/health" | j; echo

echo "### 1. status before anything (active = v1, generation = 1)"
curl -s "$BASE/status" | j; echo

echo "### 2. predict on v1"
curl -s -X POST "$BASE/predict" -H 'Content-Type: application/json' \
  -d '{"input":[0.1,-0.2,0.3,-0.4]}' | j; echo

echo "### 3. batch predict (2 x 4)"
curl -s -X POST "$BASE/predict" -H 'Content-Type: application/json' \
  -d '{"input":[[0.1,-0.2,0.3,-0.4],[-0.1,0.2,-0.3,0.4]]}' | j; echo

echo "### 4. atomically switch v1 -> v2 (note load stages + verified bytes)"
curl -s -X POST "$BASE/switch" -H 'Content-Type: application/json' \
  -d '{"version":"v2"}' | j; echo

echo "### 5. predict again: version is now v2 and logits differ"
curl -s -X POST "$BASE/predict" -H 'Content-Type: application/json' \
  -d '{"input":[0.1,-0.2,0.3,-0.4]}' | j; echo

echo "### 6. switch to the WARM-UP FAILURE fixture (expect HTTP 409 + rollback)"
curl -s -w '\nHTTP_STATUS=%{http_code}\n' -X POST "$BASE/switch" \
  -H 'Content-Type: application/json' -d '{"version":"v-bad-warmup"}' | j; echo

echo "### 7. predict after the failed switch: STILL v2 (rollback)"
curl -s -X POST "$BASE/predict" -H 'Content-Type: application/json' \
  -d '{"input":[0.1,-0.2,0.3,-0.4]}' | j; echo

echo "### 8. switch to a missing version (expect 409, still_serving=v2)"
curl -s -w '\nHTTP_STATUS=%{http_code}\n' -X POST "$BASE/switch" \
  -H 'Content-Type: application/json' -d '{"version":"ghost"}' | j; echo

echo "### 9. bad input shape (expect 400 envelope)"
curl -s -w '\nHTTP_STATUS=%{http_code}\n' -X POST "$BASE/predict" \
  -H 'Content-Type: application/json' -d '{"input":[1,2,3]}' | j; echo

echo "### 10. final status"
curl -s "$BASE/status" | j; echo
