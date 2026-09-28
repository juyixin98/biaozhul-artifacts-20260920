#!/usr/bin/env bash
# End-to-end smoke run against a freshly started compiled server.
# Verifies concrete HTTP status codes + temp reclamation, then shuts down.
set -u
cd "$(dirname "$0")/.."

PORT="${PORT:-3199}"
export PORT DATA_DIR="${DATA_DIR:-./smoke-data}"
BASE="http://127.0.0.1:${PORT}"
rm -rf "$DATA_DIR"

echo ">> building"
npm run build >/dev/null || { echo "build failed"; exit 1; }

echo ">> starting server on $PORT"
node --experimental-sqlite --no-warnings dist/src/server.js >"$DATA_DIR.server.log" 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null' EXIT

# wait for readiness
for _ in $(seq 1 50); do
  curl -sS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

fail=0
check() { # <desc> <expected_status> <actual_status>
  if [ "$2" = "$3" ]; then echo "PASS  $1 ($3)"; else echo "FAIL  $1 (expected $2 got $3)"; fail=1; fi
}

echo ">> cases"
code=$(curl -sS -o /tmp/smoke_ok.json -w '%{http_code}' -X POST "$BASE/upload" \
  -F 'title=smoke' -F 'pic=@fixtures/sample/pixel.png;type=image/png')
check "valid upload -> 201" 201 "$code"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/upload" \
  -H 'Content-Type: application/json' --data '{}')
check "wrong media type -> 415" 415 "$code"

printf -- '--B\r\nContent-Disposition: form-data; name="f"; filename="../../x"\r\nContent-Type: text/plain\r\n\r\nx\r\n--B--\r\n' >/tmp/smoke_trav.bin
code=$(curl -sS -o /tmp/smoke_trav.json -w '%{http_code}' -X POST "$BASE/upload" \
  -H 'Content-Type: multipart/form-data; boundary=B' --data-binary @/tmp/smoke_trav.bin)
check "traversal filename -> 400" 400 "$code"
grep -q PATH_TRAVERSAL_FILENAME /tmp/smoke_trav.json && echo "PASS  traversal error code" || { echo "FAIL  traversal error code"; fail=1; }

printf -- '--B\r\nContent-Disposition: form-data; name="f"\r\n\r\nabc\r\n--B' >/tmp/smoke_trunc.bin
code=$(curl -sS -o /tmp/smoke_trunc.json -w '%{http_code}' -X POST "$BASE/upload" \
  -H 'Content-Type: multipart/form-data; boundary=B' --data-binary @/tmp/smoke_trunc.bin)
check "missing terminating boundary -> 400" 400 "$code"
grep -q MISSING_TERMINATING_BOUNDARY /tmp/smoke_trunc.json && echo "PASS  truncation error code" || { echo "FAIL  truncation error code"; fail=1; }

# committed submission is queryable
SID=$(node -e "console.log(require('/tmp/smoke_ok.json').submission.id)")
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/submissions/$SID")
check "committed submission queryable -> 200" 200 "$code"

# error cases must leave no temp / committed artifacts behind
sleep 0.2
left_tmp=$(find "$DATA_DIR/tmp" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
[ "$left_tmp" = "0" ] && echo "PASS  no request temp data leaked ($left_tmp)" || { echo "FAIL  temp leaked: $left_tmp"; fail=1; }
subs=$(curl -sS "$BASE/healthz" | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>console.log(JSON.parse(s).submissions))")
[ "$subs" = "1" ] && echo "PASS  exactly one committed submission ($subs)" || { echo "FAIL  submission count $subs"; fail=1; }

echo
if [ "$fail" = "0" ]; then echo "SMOKE OK"; else echo "SMOKE FAILED"; fi
exit "$fail"
