#!/usr/bin/env bash
# Service-call examples using curl. Start the server first:
#   uvicorn ipw_ate.api:app --port 8000
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== health =="
curl -s "$BASE/health"; echo

# Build a small JSON payload from the good-overlap fixture with a tiny helper
# (python emits t/y/covariates); then POST it.
echo "== analyze good-overlap fixture =="
PYTHONPATH=src ${PYTHON:-python3} - "$BASE" <<'PY'
import json, sys, urllib.request
from ipw_ate.io_csv import load_csv
base = sys.argv[1]
t, y, x, _ = load_csv("tests/fixtures/good_overlap.csv")
payload = {"treatment": list(map(int, t)), "outcome": list(map(float, y)),
           "covariates": x.tolist(), "estimand": "ATE", "n_splits": 5,
           "request_id": "curl-good"}
req = urllib.request.Request(base + "/analyze",
                             data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
PY

echo "== analyze no-overlap fixture (expect HTTP 409) =="
PYTHONPATH=src ${PYTHON:-python3} - "$BASE" <<'PY'
import json, sys, urllib.error, urllib.request
from ipw_ate.io_csv import load_csv
base = sys.argv[1]
t, y, x, _ = load_csv("tests/fixtures/no_overlap.csv")
payload = {"treatment": list(map(int, t)), "outcome": list(map(float, y)),
           "covariates": x.tolist(), "estimand": "ATE", "n_splits": 4,
           "request_id": "curl-noov"}
req = urllib.request.Request(base + "/analyze",
                             data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req).read().decode())
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode())
PY

echo "== fetch stored run =="
curl -s "$BASE/runs/curl-good"; echo
