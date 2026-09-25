#!/usr/bin/env bash
# End-to-end demo against a locally running rsvrvd.
# Usage:
#   go run ./cmd/rsvrvd -addr 127.0.0.1:8080 &
#   ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
here="$(cd "$(dirname "$0")" && pwd)"

post() { # path json-file
  echo "── POST $1  ($2)"
  curl -sS -w '\n[HTTP %{http_code}]\n' -H 'Content-Type: application/json' \
    -X POST "$BASE$1" --data-binary @"$here/$2"
  echo
}
get() {
  echo "── GET $1"
  curl -sS -w '\n[HTTP %{http_code}]\n' "$BASE$1"
  echo
}

post /v1/resources 01-resource.json
post /v1/resources 02-resource-zero-capacity.json
post /v1/earliest 03-earliest-query.json
post /v1/reservations:batch 04-batch-mixed.json
post /v1/earliest 03-earliest-query.json
post /v1/reservations:batch 05-batch-partial-conflict.json || true
post /v1/reservations:batch 06-batch-zero-capacity.json || true
post /v1/reservations:batch 07-batch-adjacent.json
get  /v1/reservations
get  /v1/events
