#!/usr/bin/env bash
# End-to-end demo: generate sample assets, import them, create a project,
# submit a render job, wait for completion, download the MP4 and verify
# its duration with ffprobe.
#
# Usage: BASE=http://localhost:8080 ./samples/e2e.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS="$HERE/assets"
OUT="$HERE/out"
mkdir -p "$OUT"

command -v jq >/dev/null || { echo "jq is required"; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe is required"; exit 1; }

echo "==> generating sample assets"
"$HERE/generate-assets.sh" >/dev/null

echo "==> importing assets into $BASE"
while IFS=$'\t' read -r file name tags; do
  curl -sf -X POST "$BASE/api/assets" \
    -F "file=@$ASSETS/$file" -F "name=$name" -F "tags=$tags" > /dev/null
  echo "    imported $file ($tags)"
done < "$ASSETS/tags.tsv"

echo "==> creating project (12s, fade, 3 scenes)"
PROJECT=$(curl -sf -X POST "$BASE/api/projects" -H 'Content-Type: application/json' -d '{
  "name": "sample reel",
  "targetDurationSeconds": 12,
  "transition": "fade",
  "scenes": [
    {"description": "a warm sunset over the evening sky"},
    {"description": "ocean waves rolling under a blue sky"},
    {"description": "a quiet forest of green trees"}
  ]
}')
PROJECT_ID=$(echo "$PROJECT" | jq -r .id)
echo "    project id: $PROJECT_ID"

echo "==> submitting render job"
JOB=$(curl -s -X POST "$BASE/api/projects/$PROJECT_ID/jobs" \
  -H 'Content-Type: application/json' -d '{"idempotencyKey": "e2e-demo-1"}')
JOB_ID=$(echo "$JOB" | jq -r .id)
echo "    job id: $JOB_ID"

echo "==> waiting for completion"
for i in $(seq 1 120); do
  STATUS=$(curl -sf "$BASE/api/jobs/$JOB_ID" | jq -r .status)
  case "$STATUS" in
    completed) break ;;
    failed|cancelled)
      echo "job ended as $STATUS:"
      curl -sf "$BASE/api/jobs/$JOB_ID" | jq .
      exit 1 ;;
  esac
  sleep 1
done
[ "$STATUS" = completed ] || { echo "timed out waiting for job"; exit 1; }

echo "==> downloading result"
FINAL="$OUT/videoforge-job-$JOB_ID.mp4"
curl -sf -o "$FINAL" "$BASE/api/jobs/$JOB_ID/download"

DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FINAL")
echo "    saved to $FINAL"
echo "    probed duration: ${DURATION}s (target: 12s)"

awk -v d="$DURATION" 'BEGIN { if (d < 11.25 || d > 12.75) exit 1 }' \
  && echo "==> SUCCESS: duration within tolerance" \
  || { echo "==> FAILURE: duration out of tolerance"; exit 1; }
