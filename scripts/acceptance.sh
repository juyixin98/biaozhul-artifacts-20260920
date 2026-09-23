#!/usr/bin/env bash
# Run the automated acceptance suite: unit tests + a live API smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "virtualenv not found; run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.lock" >&2
  exit 1
fi

echo "== 1/3 unit & integration tests =="
$PY -m pytest tests/ -q

echo "== 2/3 boot server =="
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8137 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 50); do
  if curl -fsS http://127.0.0.1:8137/health >/dev/null 2>&1; then break; fi
  sleep 0.2
done
curl -fsS http://127.0.0.1:8137/health && echo

echo "== 3/3 sample quotes =="
for f in examples/quote_balanced.json examples/quote_extreme_imbalance.json \
         examples/quote_tiny_input.json examples/quote_zero_reserve.json; do
  echo "--- $f"
  curl -sS -X POST http://127.0.0.1:8137/quote \
    -H 'content-type: application/json' -d @"$f" \
    | $PY -c '
import sys, json
j = json.load(sys.stdin)
if "tradable" in j:
    q = j.get("quote") or {}
    print("tradable:", j["tradable"], "| error:", (j.get("error") or {}).get("code"), "| out:", q.get("amount_out_units"))
else:
    print("rejected:", j.get("error") or j.get("detail"))'
done
echo "ACCEPTANCE OK"
