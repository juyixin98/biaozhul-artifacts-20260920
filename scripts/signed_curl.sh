#!/usr/bin/env bash
# 带 HMAC 签名的 curl 辅助。
# 用法:
#   scripts/signed_curl.sh POST /api/robots examples/robot.json
#   scripts/signed_curl.sh GET  /api/snapshot
set -euo pipefail

METHOD="${1:?usage: signed_curl.sh METHOD PATH [JSON_FILE]}"
APIPATH="${2:?usage: signed_curl.sh METHOD PATH [JSON_FILE]}"
JSONFILE="${3:-}"

KEY_ID="${DISPATCH_API_KEY_ID:-demo-key-1}"
SECRET="${DISPATCH_API_SECRET:-demo-secret-key}"
export SECRET
BASE="${DISPATCH_BASE_URL:-http://127.0.0.1:8000}"
TS="$(date +%s)"
NONCE="$(python3 -c 'import uuid;print(uuid.uuid4().hex)')"

if [[ -n "$JSONFILE" ]]; then
  BODY="$(cat "$JSONFILE")"
else
  BODY=""
fi

BODY_HASH="$(printf '%s' "$BODY" | python3 -c 'import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')"
SIGN_MSG="$(printf '%s\n%s\n%s\n%s\n%s\n%s' \
  "$KEY_ID" "$TS" "$NONCE" "$METHOD" "$APIPATH" "$BODY_HASH")"
SIG="$(printf '%s' "$SIGN_MSG" | python3 -c '
import sys, hmac, hashlib, os
msg = sys.stdin.read()
print(hmac.new(os.environ["SECRET"].encode(), msg.encode(), hashlib.sha256).hexdigest())
')"

curl -sS -X "$METHOD" "${BASE}${APIPATH}" \
  -H 'Content-Type: application/json' \
  -H "X-Key-Id: $KEY_ID" \
  -H "X-Timestamp: $TS" \
  -H "X-Nonce: $NONCE" \
  -H "X-Signature: $SIG" \
  ${BODY:+--data-binary "$BODY"}
echo
