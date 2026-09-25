#!/usr/bin/env bash
# Request samples against a locally running server.
#
#   python scripts/make_artifacts.py
#   python -m modelswitch.server --artifact artifacts/v1 --port 8765 &
#   bash examples/requests.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8765}"

echo "== health =="
curl -s "$BASE/health"; echo

echo "== current version =="
curl -s "$BASE/version"; echo

echo "== predict (served by v1) =="
curl -s -X POST "$BASE/predict" \
  -H 'Content-Type: application/json' \
  -d '{"x": [[0.1, -0.2, 0.3, 0.0, 0.5, -0.1, 0.2, 0.4]]}'; echo

echo "== switch to v2 (atomic; in-flight requests keep v1) =="
curl -s -X POST "$BASE/admin/switch" \
  -H 'Content-Type: application/json' \
  -d '{"path": "artifacts/v2"}'; echo

echo "== predict (now served by v2) =="
curl -s -X POST "$BASE/predict" \
  -H 'Content-Type: application/json' \
  -d '{"x": [[0.1, -0.2, 0.3, 0.0, 0.5, -0.1, 0.2, 0.4]]}'; echo

echo "== switch to artifact that fails warm-up (rejected; v2 stays active) =="
curl -s -X POST "$BASE/admin/switch" \
  -H 'Content-Type: application/json' \
  -d '{"path": "artifacts/v3-bad-warmup"}'; echo

echo "== switch to artifact with bad checksum (rejected; v2 stays active) =="
curl -s -X POST "$BASE/admin/switch" \
  -H 'Content-Type: application/json' \
  -d '{"path": "artifacts/v4-bad-checksum"}'; echo

echo "== rollback to v1 =="
curl -s -X POST "$BASE/admin/rollback"; echo

echo "== registry stats =="
curl -s "$BASE/admin/stats"; echo
