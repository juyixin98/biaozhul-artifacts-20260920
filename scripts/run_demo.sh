#!/usr/bin/env bash
# End-to-end demo against the local IdP + gateway.
# Usage: ./scripts/run_demo.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
CURL=(curl -sS --max-time 10)
GATEWAY="http://127.0.0.1:8080"
IDP="http://127.0.0.1:8081"

echo "==> starting demo IdP on :8081 and gateway on :8080"
"$PY" scripts/demo_idp.py --port 8081 >/tmp/demo-idp.log 2>&1 &
IDP_PID=$!
trap 'kill $IDP_PID $GATEWAY_PID 2>/dev/null' EXIT

JWT_GATEWAY_CONFIG=config/issuers.json \
  "$PY" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080 \
  >/tmp/demo-gateway.log 2>&1 &
GATEWAY_PID=$!

for _ in $(seq 1 30); do
  curl -sS "$IDP/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
for _ in $(seq 1 30); do
  curl -sS "$GATEWAY/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done

mint() { "${CURL[@]}" -X POST "$IDP/mint" -H 'content-type: application/json' -d "$1"; }
extract() { "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token"])'; }

show() { # title expected-code
  echo
  echo "── $1"
}

echo
echo "== configured issuers"
"${CURL[@]}" "$GATEWAY/v1/issuers" | "$PY" -m json.tool

show "1) valid RS256 token -> 200"
TOKEN=$(mint '{"mode":"valid","issuer":"rsa"}' | extract)
"${CURL[@]}" -o /tmp/r1.json -w "HTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"
cat /tmp/r1.json | "$PY" -m json.tool

show "2) valid ES256 token (second issuer) -> 200"
TOKEN=$(mint '{"mode":"valid","issuer":"ec"}' | extract)
"${CURL[@]}" -o /tmp/r2.json -w "HTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"
cat /tmp/r2.json | "$PY" -m json.tool

show "3) alg=none -> 401 algorithm_not_allowed"
TOKEN=$(mint '{"mode":"none","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "4) RS256->HS256 algorithm confusion -> 401 algorithm_not_allowed"
TOKEN=$(mint '{"mode":"hmac-confuse","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "5) jku header pointing at attacker host -> 401 header_key_reference_forbidden"
TOKEN=$(mint '{"mode":"jku","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "6) expired token -> 401 token_expired"
TOKEN=$(mint '{"mode":"expired","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "7) nbf in the future -> 401 token_not_yet_valid"
TOKEN=$(mint '{"mode":"nbf-future","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "8) wrong audience -> 401 invalid_audience"
TOKEN=$(mint '{"mode":"bad-aud","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "9) attacker-controlled key, same kid -> 401 invalid_signature"
TOKEN=$(mint '{"mode":"attacker-rsa","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "10) unknown kid -> 401 unknown_kid (cache refresh attempted once)"
TOKEN=$(mint '{"mode":"unknown-kid","issuer":"rsa"}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$TOKEN\"}"

show "11) duplicate-kid JWKS -> 401 duplicate_kid"
TOKEN=$(mint '{"mode":"valid","issuer":"rsa","claims":{"iss":"https://demo-idp.local/dup"}}' | extract)
"${CURL[@]}" -w "\nHTTP %{http_code}\n" -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' \
  -d "{\"token\":\"$TOKEN\",\"issuer_id\":\"demo-dup\"}"

show "12) key rotation: mint old, rotate, admin refresh, mint new"
OLD=$(mint '{"mode":"valid","issuer":"rsa"}' | extract)
"${CURL[@]}" -X POST "$IDP/rotate" -H 'content-type: application/json' -d '{"issuer":"rsa"}' | "$PY" -m json.tool
"${CURL[@]}" -X POST "$GATEWAY/admin/issuers/demo-rsa/refresh" >/dev/null
NEW=$(mint '{"mode":"valid","issuer":"rsa"}' | extract)
echo "old token after rotation:"
"${CURL[@]}" -o /tmp/old.json -w "HTTP %{http_code} " -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$OLD\"}"; cat /tmp/old.json
echo
echo "new token after rotation:"
"${CURL[@]}" -o /tmp/new.json -w "HTTP %{http_code} " -X POST "$GATEWAY/v1/verify" \
  -H 'content-type: application/json' -d "{\"token\":\"$NEW\"}"; cat /tmp/new.json
echo

echo
echo "==> gateway audit log tail (note: fingerprints, never tokens)"
grep -E 'token_verification' /tmp/demo-gateway.log | tail -5 || true
