#!/usr/bin/env bash
#
# Replayable smoke demo against a real listening server.
# Demonstrates: create, weak/strong ETag conditional GET (304), optimistic
# concurrency (412), wildcard create-only (If-None-Match), and the distinct
# 404 vs 412 semantics. Logs stream as JSON lines with X-Request-Id run ids.
#
# Usage: npm run demo
set -u

PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
ID="demo-$RANDOM"
RUN="demo-$$"

say() { printf '\n=== %s ===\n' "$1"; }

say "1) Create resource (201, expect a strong ETag)"
CREATE=$(curl -sS -D - -o /tmp/rv-create-body.json \
  -H 'Content-Type: application/json' -H "X-Request-Id: ${RUN}-create" \
  -X PUT "${BASE}/resources/${ID}" \
  --data '{"name":"widget","count":1}')
echo "$CREATE" | grep -iE 'HTTP/|etag|x-resource-version'
ETAG=$(echo "$CREATE" | tr -d '\r' | awk 'tolower($1)=="etag:"{print $2}')
echo "Captured strong ETag: ${ETAG}"

say "2) Conditional GET with the current strong ETag (expect 304)"
curl -sS -D - -o /dev/null -H "X-Request-Id: ${RUN}-304" \
  -H "If-None-Match: ${ETAG}" "${BASE}/resources/${ID}" | grep -iE 'HTTP/|etag'

say "3) Conditional GET with the equivalent WEAK tag W/ (also 304 under weak comparison)"
WEAK="W/${ETAG}"
curl -sS -D - -o /dev/null -H "X-Request-Id: ${RUN}-weak304" \
  -H "If-None-Match: ${WEAK}" "${BASE}/resources/${ID}" | grep -iE 'HTTP/'

say "4) Two concurrent conditional PUTs using the same base ETag"
# Fire both in parallel; one must be 200 and the other 412.
curl -sS -D /tmp/rv-a.hdr -o /tmp/rv-a.body -H "X-Request-Id: ${RUN}-clientA" \
  -H 'Content-Type: application/json' -H "If-Match: ${ETAG}" \
  -X PUT "${BASE}/resources/${ID}" --data '{"name":"widget","count":2}' &
PID_A=$!
curl -sS -D /tmp/rv-b.hdr -o /tmp/rv-b.body -H "X-Request-Id: ${RUN}-clientB" \
  -H 'Content-Type: application/json' -H "If-Match: ${ETAG}" \
  -X PUT "${BASE}/resources/${ID}" --data '{"name":"widget","count":3}' &
PID_B=$!
wait $PID_A $PID_B
echo "clientA: $(grep -i '^HTTP/' /tmp/rv-a.hdr | tr -d '\r')"
echo "clientB: $(grep -i '^HTTP/' /tmp/rv-b.hdr | tr -d '\r')"
echo "losing client 412 body:"; cat /tmp/rv-b.body 2>/dev/null | head -c 400; cat /tmp/rv-a.body 2>/dev/null >/dev/null; echo

say "5) If-None-Match: * is create-only (second attempt -> 412)"
curl -sS -o /dev/null -w 'first  : HTTP %{http_code}\n' \
  -H "X-Request-Id: ${RUN}-wc1" -H 'Content-Type: application/json' \
  -H 'If-None-Match: *' -X PUT "${BASE}/resources/${ID}-wc" --data '{"v":1}'
curl -sS -o /dev/null -w 'second : HTTP %{http_code} (expect 412)\n' \
  -H "X-Request-Id: ${RUN}-wc2" -H 'Content-Type: application/json' \
  -H 'If-None-Match: *' -X PUT "${BASE}/resources/${ID}-wc" --data '{"v":2}'

say "6) Distinct absence semantics: GET missing -> 404, If-Match on missing -> 412"
curl -sS -o /dev/null -w 'GET absent    : HTTP %{http_code} (expect 404)\n' \
  -H "X-Request-Id: ${RUN}-404" "${BASE}/resources/does-not-exist"
curl -sS -o /dev/null -w 'PUT If-Match * : HTTP %{http_code} (expect 412)\n' \
  -H "X-Request-Id: ${RUN}-412" -H 'Content-Type: application/json' -H 'If-Match: *' \
  -X PUT "${BASE}/resources/does-not-exist" --data '{"v":1}'

say "7) Immutable version history"
curl -sS -H "X-Request-Id: ${RUN}-history" "${BASE}/resources/${ID}/versions" \
  | head -c 600; echo

echo
echo "Demo complete. Inspect the server's stdout JSON logs: every line above is"
echo "correlated by the X-Request-Id run id (${RUN}-*) and includes the condition"
echo "trace steps with expected/actual validators and the decision basis."
