#!/usr/bin/env bash
# Fire the sample inference requests at a local batchagg server (default
# 127.0.0.1:8080). Requests to the same compatibility key, launched within the
# server's --max-wait window, land in one batch.
set -u

HOST="${BATCHAGG_HOST:-127.0.0.1:8080}"
DIR="$(cd "$(dirname "$0")/requests" && pwd)"

post() {
  local file="$1"
  printf '\n--- POST %s ---\n' "$(basename "$file")"
  curl -sS -w '\nHTTP %{http_code}\n' \
    -H 'Content-Type: application/json' \
    --data-binary @"$file" \
    "http://$HOST/infer"
}

# Launch three same-key requests concurrently so they aggregate.
if [ "${1:-}" = "--concurrent" ]; then
  echo "Firing 01-03 concurrently against $HOST ..."
  for f in "$DIR"/0{1,2,3}_*.json; do
    post "$f" &
  done
  wait
  post "$DIR/04_key_from_model.json"
  post "$DIR/05_explicit_key.json"
  post "$DIR/06_partial_fail.json"
else
  for f in "$DIR"/*.json; do
    post "$f"
  done
fi
