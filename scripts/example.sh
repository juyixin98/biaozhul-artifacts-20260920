#!/usr/bin/env bash
# Run one example request against a running service (default localhost:8080).
# Usage: scripts/example.sh examples/pause-resume.json [host:port]
set -euo pipefail
cd "$(dirname "$0")/.."
FILE=${1:?usage: example.sh <request.json> [host:port]}
ADDR=${2:-127.0.0.1:8080}
curl -sS -X POST "http://$ADDR/api/run" \
  -H 'Content-Type: application/json' \
  --data-binary "@$FILE"
echo
