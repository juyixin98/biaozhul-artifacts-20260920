#!/usr/bin/env bash
# End-to-end examples against a locally running server (npm start first).
# Override BASE_URL / PORT as needed.
set -euo pipefail

PORT="${PORT:-3000}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}}"
J=(curl -s -X POST "${BASE_URL}/" -H 'content-type: application/json' -d)

echo "== health =="
curl -s "${BASE_URL}/healthz"; echo

echo "== single request =="
"${J[@]}" '{"jsonrpc":"2.0","method":"math.add","params":{"a":2,"b":3},"id":1}'; echo

echo "== mixed batch: request + notification + invalid element =="
"${J[@]}" '[{"jsonrpc":"2.0","method":"math.add","params":{"a":1,"b":1},"id":"a"},{"jsonrpc":"2.0","method":"secrets.put","params":{"name":"demo","secret":"do-not-print-me"}},7]'; echo

echo "== all notifications -> 204 =="
curl -s -o /dev/null -w 'http=%{http_code}\n' -X POST "${BASE_URL}/" \
  -H 'content-type: application/json' -d '[{"jsonrpc":"2.0","method":"echo","params":{}}]'

echo "== empty batch -> 400 / -32600 =="
curl -s -w ' [http=%{http_code}]\n' -X POST "${BASE_URL}/" \
  -H 'content-type: application/json' -d '[]'

echo "== malformed json -> 500 / -32700 =="
curl -s -w ' [http=%{http_code}]\n' -X POST "${BASE_URL}/" \
  -H 'content-type: application/json' -d '{bad json'

echo "== duplicate ids: same id, independently attributed results =="
"${J[@]}" '[{"jsonrpc":"2.0","method":"math.add","params":{"a":1,"b":2},"id":"dup"},{"jsonrpc":"2.0","method":"math.add","params":{"a":100,"b":200},"id":"dup"}]'; echo

echo "== side effect with explicit idempotency key (called twice) =="
for i in 1 2; do
  "${J[@]}" '{"jsonrpc":"2.0","method":"accounts.transfer","params":{"from":"acc-1","to":"acc-2","amount":300,"idempotencyKey":"example-key"},"id":10}'; echo
done

echo "== async accept then poll =="
ACCEPT="$("${J[@]}" '{"jsonrpc":"2.0","method":"tasks.schedule","params":{"name":"example-job","delayMs":100},"id":11}')"
echo "${ACCEPT}"
OP_SEQ="$(printf '%s' "${ACCEPT}" | grep -o '"opSeq":[0-9]*' | head -1 | cut -d: -f2)"
sleep 0.3
"${J[@]}" "{\"jsonrpc\":\"2.0\",\"method\":\"operations.get\",\"params\":{\"opSeq\":${OP_SEQ}},\"id\":12}"; echo

echo "== diagnostics =="
curl -s "${BASE_URL}/diag/requests?limit=3"; echo
