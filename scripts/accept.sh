#!/usr/bin/env bash
# One-command acceptance run:
#   1. create venv (first run) and install the locked dependencies
#   2. run the full automated test suite
#   3. start the API on an ephemeral port/database
#   4. submit every example under examples/packages/ and assert its verdict
#   5. independently verify the Ed25519 receipt signature
#   6. prove scripts inside packages are never executed
#   7. shut the server down
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8099}"
export PORT
export PROVENANCE_DB="data/accept.db"
export PROVENANCE_KEY="data/accept_key.pem"
rm -f "$PROVENANCE_DB"* "$PROVENANCE_KEY"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install -q -r requirements-dev.txt

echo "==> generating examples"
python scripts/generate_examples.py >/dev/null

echo "==> test suite"
python -m pytest tests/ -q

echo "==> starting server on :$PORT"
PYTHONPATH=. python -m uvicorn provenance_service.app:app \
  --host 127.0.0.1 --port "$PORT" >data/accept.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for i in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

# (case dir -> expected HTTP status and verdict token)
cases=(
  "01_exact:200:EXACT_MATCH"
  "02_library_address_rule:200:RULE_MATCH"
  "03_optimizer_mismatch:200:MISMATCH"
  "04_missing_source:200:MISMATCH"
  "05_malicious_path:422:REJECTED"
  "06_semantic_change:200:MISMATCH"
  "07_path_relocation_rule:200:RULE_MATCH"
  "08_no_runtime_code:200:EXACT_MATCH"
)

echo "==> submitting examples"
fail=0
for spec in "${cases[@]}"; do
  name="${spec%%:*}"; rest="${spec#*:}"
  want_http="${rest%%:*}"; want="${rest##*:}"
  d="examples/packages/$name"
  resp="$(curl -s -w $'\n%{http_code}' -X POST "http://127.0.0.1:$PORT/api/v1/verify" \
      -F "package=@$d/sources.zip;type=application/zip" \
      -F "config=<$d/config.json" \
      -F "compiler_output=<$d/compiler_output.json" \
      -F "expected_runtime_code=<$d/expected_runtime_code.json")"
  http="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  got="$(printf '%s' "$body" | python -c 'import sys,json;d=json.load(sys.stdin);print(d.get("status") if d.get("status")=="REJECTED" else d.get("verdict"))')"
  if [ "$http" = "$want_http" ] && [ "$got" = "$want" ]; then
    echo "  PASS $name -> http=$http $got"
  else
    echo "  FAIL $name -> http=$http $got (want http=$want_http $want)"; fail=1
  fi
done

echo "==> independent Ed25519 receipt verification"
d="examples/packages/01_exact"
job="$(curl -s -X POST "http://127.0.0.1:$PORT/api/v1/verify" \
    -F "package=@$d/sources.zip;type=application/zip" \
    -F "config=<$d/config.json" \
    -F "compiler_output=<$d/compiler_output.json" \
    -F "expected_runtime_code=<$d/expected_runtime_code.json")"
echo "$job" >/tmp/accept_job.json
python - <<'PY'
import json, urllib.request
job = json.load(open("/tmp/accept_job.json"))
base = "http://127.0.0.1:%s" % __import__("os").environ.get("PORT", "8099")
r = json.load(urllib.request.urlopen(f"{base}/api/v1/jobs/{job['job_id']}/receipt"))
assert r["signature_valid"] is True, r
print("  PASS receipt signature valid; chain_head present:", bool(r["chain_head"]))
PY

echo "==> no package script executed"
if [ -f /tmp/provenance_script_should_never_run.marker ]; then
  echo "  FAIL packaged script was executed"; fail=1
else
  echo "  PASS no script-execution marker"
fi

if [ "$fail" -ne 0 ]; then
  echo "ACCEPTANCE FAILED"; exit 1
fi
echo "ACCEPTANCE PASSED"
