#!/usr/bin/env bash
# End-to-end acceptance walkthrough against a running server (default :8080).
#
# Demonstrates, with visible output:
#   1. first event's attempt TIMES OUT while later events already finished,
#      yet the committed output stays in input sequence order;
#   2. per-partition buffer cap -> HTTP 429;
#   3. retries exhausted -> FAILURE placeholder committed in order;
#   4. cancellation -> cancelled event is never committed.
#
# Usage: ./examples/acceptance-demo.sh [baseUrl]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8080}"
CURL=(curl -sS -H 'Content-Type: application/json')

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
field() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(d'"$1"')'; }

say "health"
"${CURL[@]}" "$BASE/health"; echo

say "create partition demo (bufferCap=4)"
"${CURL[@]}" -X PUT "$BASE/partitions/demo" -d '{"bufferCap":4}'; echo

say "1) head event: work 3s, attempt timeout 800ms, single attempt"
HEAD=$("${CURL[@]}" -X POST "$BASE/partitions/demo/events" \
  -d '{"payload":"slow-head","delayMillis":3000,"timeoutMillis":800,"maxAttempts":1}')
echo "$HEAD"; HEAD_ID=$(echo "$HEAD" | field "['id']")

say "   tail events finish in ~50/70ms"
TAIL1=$("${CURL[@]}" -X POST "$BASE/partitions/demo/events" \
  -d '{"payload":"fast-1","delayMillis":50}')
TAIL2=$("${CURL[@]}" -X POST "$BASE/partitions/demo/events" \
  -d '{"payload":"fast-2","delayMillis":70}')
echo "$TAIL1"; echo "$TAIL2"

say "   wait 200ms: tails SUCCEEDED, head RUNNING, output still empty (buffered)"
sleep 0.2
"${CURL[@]}" "$BASE/partitions/demo/events/$(echo "$TAIL1" | field "['id']")" | field "['status']"
"${CURL[@]}" "$BASE/partitions/demo/events/$HEAD_ID" | field "['status']"
"${CURL[@]}" "$BASE/partitions/demo/results"; echo

say "   long-poll results (released once the head's timeout placeholder commits)"
"${CURL[@]}" "$BASE/partitions/demo/results?waitMillis=5000" | python3 -m json.tool

say "2) buffer cap: partition with cap=2, third submit is rejected with 429"
"${CURL[@]}" -X PUT "$BASE/partitions/cap429" -d '{"bufferCap":2}' >/dev/null
"${CURL[@]}" -X POST "$BASE/partitions/cap429/events" -d '{"delayMillis":5000}' >/dev/null
"${CURL[@]}" -X POST "$BASE/partitions/cap429/events" -d '{"delayMillis":5000}' >/dev/null
"${CURL[@]}" -w '\nHTTP %{http_code}\n' -X POST "$BASE/partitions/cap429/events" \
  -d '{"delayMillis":10}'

say "3) retry exhaustion: 3 failing attempts, then FAILURE placeholder in seq order"
"${CURL[@]}" -X PUT "$BASE/partitions/retry" -d '{}' >/dev/null
"${CURL[@]}" -X POST "$BASE/partitions/retry/events" \
  -d '{"fail":true,"delayMillis":20,"maxAttempts":3,"retryDelayMillis":100}' >/dev/null
"${CURL[@]}" -X POST "$BASE/partitions/retry/events" -d '{"delayMillis":20}' >/dev/null
"${CURL[@]}" "$BASE/partitions/retry/results?waitMillis=5000" | python3 -m json.tool

say "4) cancel: running event is interrupted and never appears in output"
"${CURL[@]}" -X PUT "$BASE/partitions/cxl" -d '{}' >/dev/null
VICTIM=$("${CURL[@]}" -X POST "$BASE/partitions/cxl/events" -d '{"delayMillis":10000}')
VID=$(echo "$VICTIM" | field "['id']")
KEEPER=$("${CURL[@]}" -X POST "$BASE/partitions/cxl/events" -d '{"delayMillis":50}')
sleep 0.1
"${CURL[@]}" -X POST "$BASE/partitions/cxl/events/$VID/cancel" \
  -d '{"reason":"demo cancel"}' | python3 -m json.tool
"${CURL[@]}" "$BASE/partitions/cxl/results?waitMillis=3000" | python3 -m json.tool
echo "(victim id=$VID must not appear in the results above)"
