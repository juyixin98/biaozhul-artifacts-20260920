#!/usr/bin/env bash
# Signed curl helper for the rollout API. Computes a real HMAC-SHA256 signature
# for METHOD PATH BODY using the same canonical string as the server.
#
# Usage:
#   scripts/sign-curl.sh POST /api/releases @examples/create_release.healthy.json
#   scripts/sign-curl.sh POST /api/releases/rel_xxx/commands/advance '{"expected_generation":0}'
#
# Env: BASE_URL (default http://localhost:8080), KEY_ID, KEY_SECRET.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
KEY_ID="${HMAC_KEY_ID:-local-dev-key}"
SECRET="${HMAC_SECRET:-dev-shared-secret-change-me}"

METHOD="${1:?method required, e.g. POST}"
PATH_ONLY="${2:?path required, e.g. /api/releases}"
BODY_ARG="${3:-}"
IDEM_KEY="${IDEMPOTENCY_KEY:-${IDEM_KEY:-}}"

# Resolve the body: @file means read the file; otherwise use the literal arg.
if [[ "$BODY_ARG" == @* ]]; then
  BODY="$(cat "${BODY_ARG#@}")"
else
  BODY="$BODY_ARG"
fi

TS="$(date +%s)"
NONCE="$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
BODY_HASH="$(printf '%s' "$BODY" | openssl dgst -sha256 -hex | awk '{print $NF}')"
CANON="$(printf '%s\n%s\n%s\n%s\n%s' "$METHOD" "$PATH_ONLY" "$TS" "$NONCE" "$BODY_HASH")"
SIG="$(printf '%s' "$CANON" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')"

HEADER="kid=\"$KEY_ID\",ts=$TS,nonce=\"$NONCE\",sig=\"$SIG\""

ARGS=(-sS -X "$METHOD" "$BASE_URL$PATH_ONLY"
      -H "Content-Type: application/json"
      -H "X-Rollout-Signature: $HEADER")
if [[ -n "$IDEM_KEY" ]]; then
  ARGS+=(-H "Idempotency-Key: $IDEM_KEY")
fi
if [[ -n "$BODY" ]]; then
  ARGS+=(--data "$BODY")
fi

curl -w '\nHTTP %{http_code}\n' "${ARGS[@]}"
