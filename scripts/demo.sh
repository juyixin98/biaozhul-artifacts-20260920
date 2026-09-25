#!/usr/bin/env bash
# End-to-end acceptance demo for the dual-stream interval join service.
#
# Builds (if needed), starts the JSON HTTP service, drives the four
# acceptance scenarios from examples/http/*.json, and saves the full
# transcript to examples/output/transcript.log.
#
# Requirements: JDK 21, Maven (set MAVEN_CMD to override), curl.
# Override the port with PORT=....
set -euo pipefail

cd "$(dirname "$0")/.."

MVN="${MAVEN_CMD:-mvn}"
PORT="${PORT:-18099}"
ROOT="http://127.0.0.1:${PORT}"
BASE="${ROOT}/api/v1"
HERE="examples/http"
OUT_DIR="examples/output"
mkdir -p "${OUT_DIR}"
LOG="${OUT_DIR}/transcript.log"
: >"${LOG}"

echo "==> Building (skip tests; run 'mvn test' separately)"
"${MVN}" -q package -DskipTests
JAR="target/dual-stream-interval-join-1.0.0.jar"

echo "==> Starting service on port ${PORT}"
java -jar "${JAR}" --port "${PORT}" >"${OUT_DIR}/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  curl -fsS "${ROOT}/health" >/dev/null 2>&1 && break
  sleep 0.2
done

say() { echo; echo "==> $*"; }
req() { # req <label> <curl-args...>
  local label="$1"; shift
  echo "### ${label}" >>"${LOG}"
  echo "\$ curl $*" >>"${LOG}"
  curl -sS -H 'Content-Type: application/json' "$@" | tee -a "${LOG}"
  echo >>"${LOG}"
}

# ---------- Scenario A: boundaries, exact-once, redelivery ----------
say "A: interval boundaries, exact-once output, duplicate redelivery"
req "A1 create job A ([0,10000], cap 3, idle 5000)" \
  -X POST "${BASE}/jobs" --data-binary "@${HERE}/create-A.json"
req "A2 push left L1@10000 L2@12000 (0 pairs yet)" \
  -X POST "${BASE}/jobs/A/events/left" --data-binary "@${HERE}/A-left.json"
req "A3 push right R1@15000 R2@22000 R3@22001 (3 pairs; R3 is 1ms past boundary)" \
  -X POST "${BASE}/jobs/A/events/right" --data-binary "@${HERE}/A-right.json"
req "A4 redeliver R2 with SAME id (status DUPLICATE, no new pair)" \
  -X POST "${BASE}/jobs/A/events/right" --data-binary "@${HERE}/A-redeliver.json"

# ---------- Scenario B: one side stalls ----------
say "B: right side stalls; left records retained; match on recovery"
req "B1 create job B (idle timeout 5000ms)" \
  -X POST "${BASE}/jobs" --data-binary "@${HERE}/create-B.json"
req "B2 right R1@10000" \
  -X POST "${BASE}/jobs/B/events/right" --data-binary "@${HERE}/B-right1.json"
req "B3 left L1@5000 L2@15000 L3@60000 (1 pair: L1,R1)" \
  -X POST "${BASE}/jobs/B/events/left" --data-binary "@${HERE}/B-left1.json"
req "B4 advance processing time 2000 -> 8000 (right now idle)" \
  -X POST "${BASE}/jobs/B/time" --data-binary "@${HERE}/B-time.json"
req "B5 status: rightIdle=true; 3 left records still buffered" \
  "${BASE}/jobs/B/status"
req "B6 right resumes R2@10000 (L1@5000 still matches: d=5000)" \
  -X POST "${BASE}/jobs/B/events/right" --data-binary "@${HERE}/B-right2.json"

# ---------- Scenario C: buffer cap ----------
say "C: buffer cap REJECT bounds memory without losing buffered records"
req "C1 create job C (cap 2, wide interval so cleanup never interferes)" \
  -X POST "${BASE}/jobs" --data-binary "@${HERE}/create-C.json"
req "C2 fill cap with L1 L2" \
  -X POST "${BASE}/jobs/C/events/left" --data-binary "@${HERE}/C-fill.json"
req "C3 L3 refused BUFFER_FULL; L1/L2 retained" \
  -X POST "${BASE}/jobs/C/events/left" --data-binary "@${HERE}/C-over.json"

# ---------- Scenario D: watermark boundary + late event ----------
say "D: explicit watermark, boundary-exact cleanup, late drop"
req "D1 create job D" \
  -X POST "${BASE}/jobs" --data-binary "@${HERE}/create-D.json"
req "D2 buffer right RA@10000 RB@15000 RC@20000 RD@35000" \
  -X POST "${BASE}/jobs/D/events/right" --data-binary "@${HERE}/D-right.json"
req "D3 inject left watermark 20000 (cleaned=2; RC at exact boundary kept)" \
  -X POST "${BASE}/jobs/D/watermark/left" --data-binary "@${HERE}/D-wm.json"
req "D4 LE@20000 matches RC; LL@19999 dropped LATE" \
  -X POST "${BASE}/jobs/D/events/left" --data-binary "@${HERE}/D-left.json"

echo
echo "==> Done. Transcript: ${LOG}"
