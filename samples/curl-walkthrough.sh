#!/usr/bin/env bash
# End-to-end walk-through against a running server (default localhost:8080).
# Usage: ./samples/curl-walkthrough.sh [baseUrl]
set -euo pipefail
B="${1:-http://localhost:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== health =="
curl -s "$B/v1/health" | j

echo; echo "== create stream 'clicks' =="
curl -s -X PUT "$B/v1/streams/clicks" -H 'Content-Type: application/json' \
  -d @"$HERE/create-stream.json" | j

echo; echo "== batch ingest (server stamps event time via injected clock) =="
curl -s -X POST "$B/v1/streams/clicks/events" -H 'Content-Type: application/json' \
  -d @"$HERE/events-batch.json" | j

echo; echo "== single ingest =="
curl -s -X POST "$B/v1/streams/clicks/events" -H 'Content-Type: application/json' \
  -d @"$HERE/event-single.json" | j

echo; echo "== point query: page:home (estimate + declared bound, no candidate promise) =="
curl -s "$B/v1/streams/clicks/count?key=page:home" | j

echo; echo "== approximate top-3 =="
curl -s "$B/v1/streams/clicks/topk?k=3" | j

echo; echo "== flush window =="
curl -s -X POST "$B/v1/streams/clicks/flush" | j

echo; echo "== query last sealed window =="
curl -s "$B/v1/streams/clicks/count?key=page:home&window=last" | j

echo; echo "== merge compatible sketches -> 200 =="
curl -s -X POST "$B/v1/merge" -H 'Content-Type: application/json' \
  -d @"$HERE/merge-compatible.json" | j

echo; echo "== merge seed mismatch -> 409 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$B/v1/merge" -H 'Content-Type: application/json' \
  -d @"$HERE/merge-seed-mismatch.json"

echo; echo "== merge width mismatch -> 409 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$B/v1/merge" -H 'Content-Type: application/json' \
  -d @"$HERE/merge-width-mismatch.json"
