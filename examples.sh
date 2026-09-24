#!/usr/bin/env bash
# Reproduce the README request examples end-to-end.
# Builds if needed, starts the server on an ephemeral port, runs every
# example request, then shuts the server down.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8090}"
[ -d out ] || ./build.sh

java -cp out phraseindex.HttpServerApp "$PORT" >/tmp/phrase-index-example.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for the port to answer.
for _ in $(seq 1 50); do
  if curl -sf "http://localhost:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

B="http://localhost:$PORT"
req() { # METHOD PATH [JSON-or-text-body] [content-type]
  local method="$1" path="$2" body="${3-}" ctype="${4-application/json}"
  echo "> $method $path ${body:+| $body}"
  if [ "$method" = GET ]; then
    curl -s "$B$path"
  else
    curl -s -X "$method" -H "Content-Type: $ctype" --data-binary "$body" "$B$path"
  fi
  echo; echo
}

echo "== health =="
req GET /health

echo "== index documents (PUT bodies are raw text) =="
req PUT /documents/1 'go go go' 'text/plain; charset=utf-8'
req PUT /documents/2 'go' 'text/plain; charset=utf-8'
req PUT /documents/3 '  hello   world  ' 'text/plain; charset=utf-8'
req PUT /documents/4 '' 'text/plain; charset=utf-8'

echo "== repeated-word phrase with positions =="
req POST /search '{"query":"\"go go\"","includePositions":true}'

echo "== cross-document boundary: lone go never extends doc1 phrase =="
req POST /search '{"query":"go AND NOT \"go go\""}'

echo "== consecutive spaces collapse; boolean with NOT and parentheses =="
req POST /search '{"query":"\"hello world\""}'
req POST /search '{"query":"NOT (go OR hello)"}'

echo "== replace doc1: old positions must vanish =="
req PUT /documents/1 'zebra' 'text/plain; charset=utf-8'
req POST /search '{"query":"go"}'
req GET /documents/1

echo "== delete doc3 and bulk upsert =="
req DELETE /documents/3
req POST /documents/bulk '{"docs":[{"id":10,"text":"quick brown fox"},{"id":11,"text":"lazy brown dog"}]}'
req POST /search '{"query":"\"brown fox\" OR dog"}'

echo "== error cases =="
req POST /search '{"query":"a AND"}'
echo "> GET /documents/999 -> status:"
curl -s -o /dev/null -w '%{http_code}\n' "$B/documents/999"
echo "> PUT /documents/abc -> status:"
curl -s -o /dev/null -w '%{http_code}\n' -X PUT --data-binary x "$B/documents/abc"
