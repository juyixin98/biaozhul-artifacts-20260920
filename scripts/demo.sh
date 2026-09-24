#!/usr/bin/env bash
# End-to-end demo: starts the service, calls every endpoint with the bundled
# example requests, runs the CLI, and stops the service. Read-only with
# respect to anything outside this directory; no network egress.
set -euo pipefail
cd "$(dirname "$0")/.."
JAR=build/jar/position-diff.jar
[ -f "$JAR" ] || scripts/build.sh >/dev/null

PORT="${1:-18080}"
java -jar "$JAR" serve "$PORT" >/tmp/position-diff-demo.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
sleep 1

base="http://127.0.0.1:$PORT"
echo "== health =="
curl -s "$base/health"; echo

echo "== diff (basic) =="
curl -s -X POST "$base/api/diff" -H 'Content-Type: application/json' \
    --data @examples/diff-basic.json | jq '{optimal, degraded, stats, unifiedDiff}'

echo "== diff (CRLF via line array) =="
curl -s -X POST "$base/api/diff" -H 'Content-Type: application/json' \
    --data @examples/diff-lines-and-crlf.json | jq '{optimal, degraded, editDistance:.stats.editDistance}'

echo "== diff (budget exhausted -> degraded) =="
curl -s -X POST "$base/api/diff" -H 'Content-Type: application/json' \
    --data @examples/diff-degraded.json | tee /tmp/demo-degraded.json \
    | jq '{optimal, degraded, degradedReason, editDistance:.stats.editDistance, shortestDistance:.stats.shortestDistance}'

echo "== apply the degraded script -> exact target =="
jq '{oldText: .oldText, ops: .ops}' examples/diff-degraded.json > /tmp/demo-apply-in.json
jq -s '.[0] * {ops: .[1].ops} | {oldText, ops}' /tmp/demo-apply-in.json /tmp/demo-degraded.json \
  | curl -s -X POST "$base/api/apply" -H 'Content-Type: application/json' --data @- \
  | jq '{applied, matchesTarget:(.newText==("L9\nL8\nL7\nL6\nL5\nL4\nL3\nL2\nL1\nL0\n"))}'

echo "== apply (hand-written script) =="
curl -s -X POST "$base/api/apply" -H 'Content-Type: application/json' \
    --data @examples/apply-request.json | jq '{applied, newText}'

echo "== search (synthetic corpus) =="
curl -s -X POST "$base/api/search" -H 'Content-Type: application/json' \
    --data @examples/search-request.json | jq '{query, queryTerms, hits:[.hits[]|{docId,score,lineNumber,line}]}'

echo "== CLI search =="
java -jar "$JAR" search CRLF 3 | jq '{hits:[.hits[]|{docId,lineNumber,line}]}'
