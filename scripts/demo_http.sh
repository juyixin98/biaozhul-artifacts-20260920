#!/usr/bin/env bash
# Start the 2SLS service locally and exercise it with curl.
# Requires: curl. Uses an isolated temporary SQLite database.
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

echo ">> preparing local service"
# Pick an ephemeral free port rather than assuming 8000/80xx are free.
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export TWOSLS_DB_PATH="$(mktemp -d)/twosls-http.db"
echo ">> starting service on http://127.0.0.1:${PORT} (db: $TWOSLS_DB_PATH)"
python3 -m uvicorn twosls.api:app --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  body="$(curl -s "http://127.0.0.1:${PORT}/health" || true)"
  echo "$body" | grep -q '"weak_f"' && break
  sleep 0.2
done

echo
echo ">> health"
curl -s "http://127.0.0.1:${PORT}/health" | python3 -m json.tool

echo
echo ">> generating a synthetic strong-IV request"
PAYLOAD=$(PYTHONPATH=src:. python3 - <<'PY'
import json
import numpy as np
from experiments.dgp import strong_iv_sample
from twosls.contract import EstimationOptions, InstrumentValidityClaim, ModelSpec
s = strong_iv_sample()
cols = {k: np.asarray(v, dtype=float).tolist() for k, v in s.columns.items()}
cols["const"] = [1.0] * len(cols["y"])
req = {
    "request_id": "curl-demo-strong",
    "columns": cols,
    "spec": ModelSpec(dependent="y", endogenous=["x_end"],
                      included_exogenous=["w1", "const"],
                      excluded_instruments=["z1", "z2"]).model_dump(),
    "options": EstimationOptions().model_dump(),
    "validity_claim": InstrumentValidityClaim(
        exclusion_restriction_asserted=True,
        rationale="synthetic: Z independent of structural error").model_dump(),
}
print(json.dumps(req))
PY
)

echo ">> POST /api/v1/iv/estimate (status/coefficient summary only)"
echo "$PAYLOAD" | curl -s -X POST http://127.0.0.1:${PORT}/api/v1/iv/estimate \
  -H "Content-Type: application/json" -H "X-Request-ID: curl-demo" \
  --data-binary @- \
  | python3 -c "
import json, sys
b = json.load(sys.stdin)
print('status      :', b['status'])
for c in b['coefficients']:
    print(f\"  {c['name']:8s} {c['estimate']:+.4f} (se {c['std_error']:.4f})\")
print('first-stage F:', round(b['first_stage'][0]['f_statistic'], 2))
print('decision    :')
for line in b['decision_summary']['why']:
    print('  -', line)
"

echo
echo ">> GET /api/v1/runs/curl-demo-strong (persisted evidence)"
curl -s http://127.0.0.1:${PORT}/api/v1/runs/curl-demo-strong | python3 -m json.tool
