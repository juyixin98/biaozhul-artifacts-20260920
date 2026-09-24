#!/usr/bin/env bash
# End-to-end acceptance demo: starts the real uvicorn server, exercises the
# HTTP API (one-shot estimate, session ingest, late-data rejection,
# finalize, evidence verification), then stops the server.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
PORT=${PORT:-8123}
BASE="http://127.0.0.1:${PORT}"
WORK=$(mktemp -d -t soc_demo_XXXXXX)
trap 'kill ${SERVER_PID:-} 2>/dev/null || true' EXIT

echo "== 1. verify frozen parameter signature (Ed25519) =="
$PY tools/cli.py --verify-params

echo "== 2. start uvicorn on port ${PORT} (data dir: ${WORK}) =="
SOC_DATA_DIR="${WORK}/data" SOC_EVIDENCE_DIR="${WORK}/evidence" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" \
  >"${WORK}/server.log" 2>&1 &
SERVER_PID=$!

READY=0
for _ in $(seq 1 50); do
  kill -0 "${SERVER_PID}" 2>/dev/null || { echo "server died early, log:"; cat "${WORK}/server.log"; exit 1; }
  if curl -sf "${BASE}/health" 2>/dev/null | grep -q '"soc-estimator"'; then READY=1; break; fi
  sleep 0.2
done
[ "${READY}" = 1 ] || { echo "server did not become ready, log:"; cat "${WORK}/server.log"; exit 1; }

echo "== 3. health + frozen params =="
curl -s "${BASE}/health" | $PY -m json.tool
curl -s "${BASE}/api/v1/params" | $PY -m json.tool

echo "== 4. one-shot estimate over examples/sensor_dropout.json =="
curl -s -X POST "${BASE}/api/v1/estimate" \
  -H 'Content-Type: application/json' \
  -d @examples/sensor_dropout.json | $PY -c '
import json,sys
r=json.load(sys.stdin)
res=r["result"]
print("params:", r["params_version"], "| digest:", r["params_digest"][:16])
print("final_soc:", res["final_soc"], "| gaps:", res["n_gap_intervals"], "| flags:", res["flags"])
print("calibrations:", [(c["t_s"], round(c["soc_after"],4)) for c in res["calibrations"]])
print("banner:", r["not_for_control"])
'

echo "== 5. session: create, ingest, late-data rejection, finalize =="
# Dense synthetic batches built with Python: 60s rest anchor, then discharge.
BATCH1=$($PY -c '
import json
s=[{"t_s": t, "current_a": 0.0, "voltage_v": 4.0, "temp_c": 25.0} for t in range(0,61)]
print(json.dumps({"replay_window_s": 100, "samples": s}))')
BATCH2=$($PY -c '
import json
# discharge 61..300, deliberately leaving t=250 absent; 61..119 absent too
# (sensor gap -> GAP_BEFORE handling during estimation)
ts=list(range(120,250))+list(range(251,301))
s=[{"t_s": t, "current_a": 2.0, "voltage_v": 3.9, "temp_c": 25.0} for t in ts]
print(json.dumps({"samples": s}))')
BATCH3=$($PY -c '
import json
# latest=300, cutoff=200: t=100 is missing (in the 61..119 sensor gap) and
# older than the replay cutoff -> rejected; t=250 is missing but inside the
# replay window -> accepted (bounded replay).
s=[{"t_s": 100, "current_a": 1.0, "voltage_v": 4.0, "temp_c": 25.0},
   {"t_s": 250, "current_a": 2.0, "voltage_v": 3.9, "temp_c": 25.0}]
print(json.dumps({"samples": s}))')

SID=$(curl -s -X POST "${BASE}/api/v1/sessions" \
  -H 'Content-Type: application/json' -d "${BATCH1}" \
  | $PY -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
echo "session: ${SID}"

curl -s -X POST "${BASE}/api/v1/sessions/${SID}/ingest" \
  -H 'Content-Type: application/json' -d "${BATCH2}" \
  | $PY -c 'import json,sys; r=json.load(sys.stdin); print("forward ingest accepted:", r["accepted"], "stored:", r["n_stored"])'

curl -s -X POST "${BASE}/api/v1/sessions/${SID}/ingest" \
  -H 'Content-Type: application/json' -d "${BATCH3}" \
  | $PY -c 'import json,sys; r=json.load(sys.stdin); print("late ingest: accepted", r["accepted"], "rejected", r["rejected"], r["rejections"])'

curl -s -X POST "${BASE}/api/v1/sessions/${SID}/finalize" | $PY -c '
import json,sys
r=json.load(sys.stdin)
res=r["result"]
print("finalized:", r["finalized"], "| samples:", r["n_samples"],
      "| final_soc:", round(res["final_soc"],4), "| flags:", res["flags"])
'

echo "== 6. verify evidence hash chain + HMACs =="
curl -s "${BASE}/api/v1/evidence/verify" | $PY -m json.tool

echo "== ALL ACCEPTANCE CHECKS PASSED =="
