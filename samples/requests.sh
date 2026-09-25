#!/usr/bin/env bash
# Sample requests against a locally running server (default port 8080).
# Usage: ./samples/requests.sh [port]
set -euo pipefail
PORT="${1:-8080}"
BASE="http://127.0.0.1:${PORT}"
SAMPLES="$(cd "$(dirname "$0")" && pwd)"

run() {
  local title="$1" path="$2" file="$3"
  echo "== ${title} =="
  curl -s -w '\nHTTP %{http_code}\n' -X POST "${BASE}${path}" \
    -H 'Content-Type: application/json' --data-binary "@${SAMPLES}/${file}"
  echo
}

run "negative rounding: -1500 ms -> s, FLOOR" /convert convert-floor.request.json
run "overflow: Long.MAX_VALUE s -> ns"        /convert convert-overflow.request.json
run "fractional seconds text -> ns"           /parse   parse-fractional.request.json
run "sub-nanosecond precision rejected"       /parse   parse-invalid-precision.request.json
run "format: -1500 ms -> decimal text"        /format  format.request.json

echo "== meta (tzdb version) =="
curl -s -w '\nHTTP %{http_code}\n' "${BASE}/meta"
