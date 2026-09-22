#!/usr/bin/env bash
#
# End-to-end walkthrough against a running VideoForge API:
#   import samples -> create project -> submit -> poll -> download -> ffprobe
#
# Usage:
#   scripts/e2e-demo.sh [base-url] [sample-dir]
#
# Requires: curl, jq, ffmpeg/ffprobe. Generate samples first:
#   scripts/generate-samples.sh
#
set -euo pipefail

BASE="${1:-http://localhost:8080}"
SAMPLE_DIR="${2:-sample-assets}"
need() { command -v "$1" >/dev/null 2>&1 || { echo "error: $1 is required" >&2; exit 1; }; }
need curl; need jq; need ffprobe

upload() { # file tags...
  local file="$1"; shift
  local tags
  tags=$(IFS=,; echo "$*")
  curl -sS -X POST "$BASE/api/materials" \
    -F "file=@${SAMPLE_DIR}/${file}" \
    -F "tags=${tags}" | jq -r '.id'
}

echo ">> 1/5 importing materials"
M_OCEAN=$(upload ocean.png  ocean sea blue)
M_SUNSET=$(upload sunset.png sunset sky orange)
M_FOREST=$(upload forest.png forest green)
M_CITY=$(upload city.png city urban gray)
M_DESERT=$(upload desert.png desert sand yellow)
echo "   materials: $M_OCEAN $M_SUNSET $M_FOREST $M_CITY $M_DESERT"

echo ">> 2/5 creating project (8 s, fade, 3 scenes)"
PROJECT=$(curl -sS -X POST "$BASE/api/projects" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "demo-reel",
    "targetDurationMs": 8000,
    "transition": "fade",
    "scenes": [
      {"description": "Opening over the ocean", "keywords": ["ocean"]},
      {"description": "Middle in the forest", "keywords": ["forest"]},
      {"description": "Closing at sunset", "keywords": ["sunset"]}
    ]
  }')
PID=$(echo "$PROJECT" | jq -r '.id')
echo "   project: $PID"

echo ">> 3/5 dry-run match report"
curl -sS "$BASE/api/projects/$PID/match-report" | jq '{ready, scenes: [.scenes[] | {position, matched, materialFilename, score}]}'

echo ">> 4/5 submitting (idempotency key demo-reel-001)"
SUBMIT1=$(curl -sS -w '\n%{http_code}' -X POST "$BASE/api/projects/$PID/jobs" \
  -H 'Content-Type: application/json' -d '{"submissionKey":"demo-reel-001"}')
JID=$(echo "$SUBMIT1" | head -1 | jq -r '.id')
echo "   first submit HTTP $(echo "$SUBMIT1" | tail -1), job: $JID"

echo ">>    replaying the same submission key returns the same job"
SUBMIT2=$(curl -sS -w '\n%{http_code}' -X POST "$BASE/api/projects/$PID/jobs" \
  -H 'Content-Type: application/json' -d '{"submissionKey":"demo-reel-001"}')
echo "   replay HTTP $(echo "$SUBMIT2" | tail -1), job: $(echo "$SUBMIT2" | head -1 | jq -r '.id')"

echo ">> 5/5 polling status"
while :; do
  JOB=$(curl -sS "$BASE/api/jobs/$JID")
  STATUS=$(echo "$JOB" | jq -r '.status')
  echo "   status=$STATUS"
  case "$STATUS" in
    completed|failed|cancelled) break ;;
  esac
  sleep 1
done

if [ "$STATUS" != completed ]; then
  echo ">> job did not complete:"
  echo "$JOB" | jq .
  curl -sS "$BASE/api/jobs/$JID/attempts" | jq .
  exit 1
fi

OUT="result-$JID.mp4"
curl -sS -o "$OUT" "$BASE/api/jobs/$JID/download"
echo ">> downloaded $OUT"
echo ">> ffprobe verification:"
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT"
echo ">> attempts (logs, errors, material versions):"
curl -sS "$BASE/api/jobs/$JID/attempts" | jq '.[] | {attemptNo, outcome, materialManifest}'
