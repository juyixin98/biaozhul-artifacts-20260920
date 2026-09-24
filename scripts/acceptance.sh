#!/usr/bin/env bash
# End-to-end acceptance check: rebuilds, starts the server with its clock
# pinned to the examples' issuance time, and submits every sample envelope.
# Requires Go 1.22+ and curl. No third-party services are contacted.
set -euo pipefail

cd "$(dirname "$0")/.."

# Pick an ephemeral loopback port to avoid colliding with stale servers.
ADDR="127.0.0.1:$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
BASE="http://${ADDR}"

go build -o /tmp/attestation-server-acceptance ./cmd/attestation-server
go test ./...

start_server() {
  /tmp/attestation-server-acceptance "$@" -addr "$ADDR" >/tmp/attestation-server.log 2>&1 &
  SRV_PID=$!
  trap 'kill "$SRV_PID" 2>/dev/null || true' EXIT
  for _ in $(seq 1 50); do
    if ! kill -0 "$SRV_PID" 2>/dev/null; then
      echo "server exited early:"; cat /tmp/attestation-server.log; exit 1
    fi
    curl -sf "$BASE/healthz" >/dev/null && return 0
    sleep 0.1
  done
  echo "server did not become ready:"; cat /tmp/attestation-server.log; exit 1
}

start_server -now 2026-09-15T12:00:00Z

check() {
  local file="$1" want_accept="$2" want_code="${3:-}"
  local body status
  body=$(curl -s -o /tmp/resp.json -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' --data-binary @"$file" "$BASE/verify")
  status=$body
  local accept code
  accept=$(python3 -c 'import json;print(str(json.load(open("/tmp/resp.json"))["accepted"]).lower())' 2>/dev/null || echo parse-error)
  code=$(python3 -c 'import json;print(json.load(open("/tmp/resp.json")).get("code",""))' 2>/dev/null || echo parse-error)
  if [[ "$accept" != "$want_accept" || ( -n "$want_code" && "$code" != "$want_code" ) ]]; then
    echo "FAIL $file: http=$status accepted=$accept code=$code (want accepted=$want_accept code=$want_code)"
    cat /tmp/resp.json
    exit 1
  fi
  printf 'ok  %-28s http=%s accepted=%s code=%s\n' "$file" "$status" "$accept" "$code"
}

echo "== fixed-time sample envelopes =="
check examples/valid-envelope.json    true
check examples/tampered-digest.json   false BAD_SIGNATURE
check examples/old-key.json           false KEY_EXPIRED
check examples/extra-field.json       false INVALID_STATEMENT
check examples/cross-purpose.json     false BAD_SIGNATURE
check examples/untrusted-source.json  false UNTRUSTED_SOURCE
check examples/noncanonical-wire.json false NON_CANONICAL_PAYLOAD
check examples/duplicate-key.json     false DUPLICATE_JSON_KEY

echo "== replay: same valid envelope submitted twice =="
check examples/valid-envelope.json false REPLAYED_NONCE

echo "== live signing with a real clock (fresh nonce + timestamp) =="
kill "$SRV_PID" 2>/dev/null || true
wait "$SRV_PID" 2>/dev/null || true
start_server
go run ./cmd/sign-attestation -out /tmp/fresh-envelope.json
body=$(curl -s -o /tmp/resp.json -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' --data-binary @/tmp/fresh-envelope.json "$BASE/verify")
accept=$(python3 -c 'import json;print(str(json.load(open("/tmp/resp.json"))["accepted"]).lower())')
[[ "$body" == "200" && "$accept" == "true" ]] || { echo "FAIL live envelope: http=$body"; cat /tmp/resp.json; exit 1; }
echo "ok  live fresh envelope          http=200 accepted=True"

# Replay the fresh envelope.
body=$(curl -s -o /tmp/resp.json -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' --data-binary @/tmp/fresh-envelope.json "$BASE/verify")
code=$(python3 -c 'import json;print(json.load(open("/tmp/resp.json")).get("code",""))')
[[ "$body" == "409" && "$code" == "REPLAYED_NONCE" ]] || { echo "FAIL live replay: http=$body"; cat /tmp/resp.json; exit 1; }
echo "ok  live replay rejected         http=409 code=REPLAYED_NONCE"

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
