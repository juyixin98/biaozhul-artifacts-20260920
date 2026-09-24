#!/usr/bin/env bash
# End-to-end demo against a locally running service (default :8000).
# Usage: ./scripts/demo.sh [base_url]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

echo "== health =="
curl -sf "$BASE/health" | python3 -m json.tool

echo "== frozen params (HMAC verified at startup) =="
curl -sf "$BASE/v1/params" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['manifest']['version'], d['sha256'][:16]+'...', 'verified:', d['signature_verified'])"

echo "== create demo session =="
curl -sf -X POST "$BASE/v1/sessions" -H 'Content-Type: application/json' \
  -d @examples/create_session_demo.json | python3 -m json.tool

echo "== ingest example telemetry (rest/discharge/gap/cold/charge/rest) =="
curl -sf -X POST "$BASE/v1/sessions/demo/samples" -H 'Content-Type: application/json' \
  -d @examples/sample_telemetry.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['summary'], indent=1))"

echo "== latest SOC =="
curl -sf "$BASE/v1/sessions/demo/soc" | python3 -m json.tool

echo "== replay window: create replay-demo session and ingest =="
curl -sf -X POST "$BASE/v1/sessions" -H 'Content-Type: application/json' \
  -d @examples/create_session_replay.json > /dev/null
curl -sf -X POST "$BASE/v1/sessions/replay-demo/samples" -H 'Content-Type: application/json' \
  -d @examples/replay_telemetry.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('accepted:', d['accepted'], 'anchor_verified:', d['anchor_verified'])"

echo "== late data: one in-window (accepted) + one stale (rejected) =="
curl -s -X POST "$BASE/v1/sessions/replay-demo/samples" -H 'Content-Type: application/json' \
  -d @examples/late_samples.json | python3 -m json.tool

echo "== stale-only replay is rejected with 409 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/v1/sessions/replay-demo/samples" \
  -H 'Content-Type: application/json' \
  -d '{"samples": [{"t_s": 55.0, "current_a": 0.0, "voltage_v": 3.78, "temp_c": 25.0}]}'

echo "== evidence chain =="
curl -sf "$BASE/v1/sessions/demo/evidence" | python3 -c "import json,sys; d=json.load(sys.stdin); print('chain_valid:', d['chain_valid'], 'records:', len(d['records']))"

echo "demo OK"
