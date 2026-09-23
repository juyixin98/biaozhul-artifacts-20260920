#!/usr/bin/env bash
# Example requests against the Slang JSON service.
# Usage:
#   python -m slang.cli serve --port 8791
#   bash requests/curl_examples.sh
set -e
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8791}
DIR="$(cd "$(dirname "$0")" && pwd)"

post() {
  local path=$1 file=$2
  echo "==> POST $path  ($file)"
  curl -s -X POST "http://$HOST:$PORT$path" \
    -H 'Content-Type: application/json' \
    --data-binary "@$DIR/$file"
  echo; echo
}

curl -s "http://$HOST:$PORT/health"; echo; echo
post /parse   analyze_shadow.json
post /analyze analyze_shadow.json
post /lower   lower_escape.json
post /run/ir  run_recursion.json
post /run/source run_recursion.json
post /compare compare_shared.json
post /run/ir  error_unbound.json || true
