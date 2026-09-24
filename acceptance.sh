#!/usr/bin/env bash
# End-to-end acceptance check: installs locked deps, runs the full test
# suite, starts the API server, uploads the example snapshot and exercises
# representative queries (allow / default-deny / named port / ipBlock
# exclusion / namespace label change / unsupported protocol).
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8791}"
BASE="http://127.0.0.1:${PORT}"

echo "== 1/5  create venv and install locked dependencies =="
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "== 2/5  run automated tests =="
.venv/bin/python -m pytest tests/ -q

echo "== 3/5  batch CLI over examples/scenario.json (informational; the
        scenario deliberately contains denied flows and one SCTP error) =="
.venv/bin/python -m app.cli examples/scenario.json > /tmp/netpol-cli.json || true
.venv/bin/python - <<'PY'
import json
doc = json.load(open("/tmp/netpol-cli.json"))
for r in doc["results"]:
    status = r.get("error", "reachable" if r.get("reachable") else "DENIED")
    print(f"   [{status}] query #{r['index']}")
PY

echo "== 4/5  start API server on port ${PORT} =="
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
  > /tmp/netpol-acceptance.log 2>&1 &
SRV=$!
trap 'kill ${SRV} 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  curl -sf "${BASE}/health" >/dev/null && break
  sleep 0.2
done
curl -s "${BASE}/health" | .venv/bin/python -m json.tool

echo "== 5/5  run HTTP acceptance queries =="
.venv/bin/python scripts/acceptance_queries.py "${BASE}"

echo "ALL ACCEPTANCE CHECKS PASSED"
