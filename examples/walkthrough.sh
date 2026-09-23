#!/usr/bin/env bash
# walkthrough.sh — end-to-end acceptance walkthrough for the revocable
# credential index. Requires a running server (default :8090), curl and jq.
#
# It demonstrates, with REAL crypto and REAL PostgreSQL state:
#   1. issuer creation (synthetic Ed25519 key)
#   2. credential issuance with a real signature
#   3. VALID / PURPOSE_MISMATCH / CONTENT_MISMATCH verdicts
#   4. snapshot-keyed cache (cache_hit flips true, then false after revoke)
#   5. key rotation: signatures on both sides of rotation verify
#   6. revoke + verify return the SAME snapshot number
#   7. historical replay: VALID before revocation effective time, REVOKED after
#   8. concurrent revocations: exactly one winner, all others 409
#   9. expiry boundary: a short-lived credential goes EXPIRED
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8090}"
FAILURES=0

c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn() { printf '\033[32m%s\033[0m\n' "$*"; }
c_bold() { printf '\033[1m%s\033[0m\n' "$*"; }

post() { # post <url> <json>  -> prints "HTTP_CODE\nBODY"
  curl -sS -w '\n%{http_code}' -H 'Content-Type: application/json' \
    -X POST "$1" -d "$2"
}

# split the last line (http code) from the body; sets CODE and BODY
split_resp() {
  CODE=$(printf '%s' "$1" | tail -n1)
  BODY=$(printf '%s' "$1" | sed '$d')
}

expect_code() { # expect_code <want> <step>
  if [[ "$CODE" != "$1" ]]; then
    c_red "FAIL [$2]: expected HTTP $1, got $CODE: $BODY"
    FAILURES=$((FAILURES + 1))
    return 1
  fi
}

verdict_of() { printf '%s' "$BODY" | jq -r '.verdict // empty'; }
snap_of()    { printf '%s' "$BODY" | jq -r '.snapshot // empty'; }

expect_verdict() { # expect_verdict <want> <step>
  local got
  got=$(verdict_of)
  if [[ "$got" != "$1" ]]; then
    c_red "FAIL [$2]: expected verdict $1, got '$got' ($BODY)"
    FAILURES=$((FAILURES + 1))
    return 1
  fi
  c_grn "ok [$2]: $got (snapshot $(snap_of))"
}

c_bold "== health =="
curl -sS "$BASE_URL/healthz" | jq .

c_bold "== 1) create synthetic issuer =="
split_resp "$(post "$BASE_URL/v1/issuers" '{"name":"walkthrough-ca"}')"
expect_code 201 "create issuer"
ISSUER=$(printf '%s' "$BODY" | jq -r '.issuer.issuer_id')
KID1=$(printf '%s' "$BODY" | jq -r '.genesis_key.kid')
echo "issuer=$ISSUER genesis_kid=$KID1"

c_bold "== 2) issue a credential (real Ed25519 signature) =="
ISSUE_JSON=$(jq -n --arg iss "$ISSUER" \
  '{issuer_id:$iss, subject:"synthetic-subject-0001", purpose:"door-access",
    not_before:(now|todate), expires_at:((now+3600)|todate),
    content:{zone:"north", level:4}}')
split_resp "$(post "$BASE_URL/v1/credentials" "$ISSUE_JSON")"
expect_code 201 "issue"
CRED=$(printf '%s' "$BODY" | jq -r '.credential_id')
ISSUED_AT=$(printf '%s' "$BODY" | jq -r '.issued_at')
SIG=$(printf '%s' "$BODY" | jq -r '.signature_b64')
echo "credential=$CRED signature_b64=${SIG:0:24}…"

c_bold "== 3) verify: VALID, purpose mismatch, content mismatch =="
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c, expected_purpose:"door-access",
                            content:{zone:"north", level:4}}')")"
expect_code 200 "verify valid"
expect_verdict VALID "verify valid"

split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c, expected_purpose:"vpn"}')")"
expect_verdict PURPOSE_MISMATCH "purpose mismatch"

split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c, content:{zone:"north", level:5}}')")"
expect_verdict CONTENT_MISMATCH "content mismatch"

c_bold "== 4) second identical verify is served from the snapshot cache =="
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c, expected_purpose:"door-access",
                            content:{zone:"north", level:4}}')")"
HIT=$(printf '%s' "$BODY" | jq -r '.cache_hit')
[[ "$HIT" == "true" ]] && c_grn "ok [cache hit before revoke]" || {
  c_red "FAIL [cache hit before revoke]: cache_hit=$HIT"; FAILURES=$((FAILURES+1)); }

c_bold "== 5) key rotation: signatures on both sides verify =="
split_resp "$(post "$BASE_URL/v1/issuers/$ISSUER/keys/rotate" '{}')"
expect_code 201 "rotate"
KID2=$(printf '%s' "$BODY" | jq -r '.keypair.kid')
RETIRED=$(printf '%s' "$BODY" | jq -r '.previous_key.retired_at')
echo "new_kid=$KID2 previous_retired_at=$RETIRED"
[[ "$KID1" != "$KID2" && "$RETIRED" != "null" ]] || {
  c_red "FAIL [rotation metadata]"; FAILURES=$((FAILURES+1)); }

ISSUE2_JSON=$(jq -n --arg iss "$ISSUER" \
  '{issuer_id:$iss, subject:"synthetic-subject-0002", purpose:"door-access",
    not_before:(now|todate), expires_at:((now+3600)|todate),
    content:{zone:"south"}}')
split_resp "$(post "$BASE_URL/v1/credentials" "$ISSUE2_JSON")"
expect_code 201 "issue after rotate"
CRED2=$(printf '%s' "$BODY" | jq -r '.credential_id')
GOT_KID=$(printf '%s' "$BODY" | jq -r '.kid')
[[ "$GOT_KID" == "$KID2" ]] && c_grn "ok [new credential signed by rotated key]" || {
  c_red "FAIL [new credential kid=$GOT_KID want $KID2]"; FAILURES=$((FAILURES+1)); }

split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED2" '{credential_id:$c, expected_purpose:"door-access"}')")"
expect_verdict VALID "cred2 under new key"
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c, expected_purpose:"door-access"}')")"
expect_verdict VALID "cred1 still verifies under retired old key"

c_bold "== 6) retire blocks issuance until a new rotation (422 then 201) =="
split_resp "$(post "$BASE_URL/v1/issuers/$ISSUER/keys/retire" '{}')"
expect_code 200 "retire"
split_resp "$(post "$BASE_URL/v1/credentials" "$ISSUE2_JSON")"
expect_code 422 "issue while no active key"
split_resp "$(post "$BASE_URL/v1/issuers/$ISSUER/keys/rotate" '{}')"
expect_code 201 "re-key after retire"

c_bold "== 7) revoke: revoke and verify return the SAME snapshot number =="
split_resp "$(post "$BASE_URL/v1/credentials/$CRED/revoke" \
  "$(jq -n '{reason:"synthetic: badge reported lost"}')")"
expect_code 201 "revoke"
REV_SNAP=$(snap_of)
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c}')")"
expect_verdict REVOKED "current verify after revoke"
VER_SNAP=$(snap_of)
HIT=$(printf '%s' "$BODY" | jq -r '.cache_hit')
[[ "$HIT" == "false" ]] && c_grn "ok [no stale cache after revoke]" || {
  c_red "FAIL [stale cache after revoke]: cache_hit=$HIT"; FAILURES=$((FAILURES+1)); }
if [[ "$REV_SNAP" == "$VER_SNAP" ]]; then
  c_grn "ok [same snapshot]: revoke=$REV_SNAP verify=$VER_SNAP"
else
  c_red "FAIL [snapshot mismatch]: revoke=$REV_SNAP verify=$VER_SNAP"
  FAILURES=$((FAILURES + 1))
fi

c_bold "== 8) historical replay does NOT use current state =="
# as_of = issued_at (before the revocation existed) -> VALID
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" --arg t "$ISSUED_AT" '{credential_id:$c, as_of:$t}')")"
expect_verdict VALID "replay at issuance (pre-revoke)"
# as_of omitted -> REVOKED (already asserted); future as_of also REVOKED
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED" '{credential_id:$c, as_of:"2030-01-01T00:00:00Z"}')")"
expect_verdict REVOKED "replay in the future"
# double revoke is rejected
split_resp "$(post "$BASE_URL/v1/credentials/$CRED/revoke" '{"reason":"again"}')"
expect_code 409 "duplicate revoke"

c_bold "== 9) concurrent revocations: exactly one winner =="
ISSUE3_JSON=$(jq -n --arg iss "$ISSUER" \
  '{issuer_id:$iss, subject:"race-target", purpose:"p",
    not_before:(now|todate), expires_at:((now+3600)|todate), content:{}}')
split_resp "$(post "$BASE_URL/v1/credentials" "$ISSUE3_JSON")"
expect_code 201 "issue race target"
CRED3=$(printf '%s' "$BODY" | jq -r '.credential_id')

TMP=$(mktemp -d)
for i in $(seq 1 12); do
  curl -sS -o "$TMP/$i.body" -w '%{http_code}' \
    -H 'Content-Type: application/json' -X POST \
    "$BASE_URL/v1/credentials/$CRED3/revoke" -d '{"reason":"race"}' \
    > "$TMP/$i.code" &
done
wait
WINS=$(grep -lxF 201 "$TMP"/*.code 2>/dev/null | wc -l | tr -d ' ')
LOSSES=$(grep -lxF 409 "$TMP"/*.code 2>/dev/null | wc -l | tr -d ' ')
rm -rf "$TMP"
if [[ "$WINS" == 1 && "$LOSSES" == 11 ]]; then
  c_grn "ok [concurrent revoke]: 1 winner, 11 conflicts (409)"
else
  c_red "FAIL [concurrent revoke]: winners=$WINS conflicts=$LOSSES (want 1/11)"
  FAILURES=$((FAILURES + 1))
fi

c_bold "== 10) expiry boundary (short-lived credential) =="
SOON_JSON=$(jq -n --arg iss "$ISSUER" \
  '{issuer_id:$iss, subject:"short-lived", purpose:"p",
    not_before:(now|todate), expires_at:((now+3)|todate), content:{}}')
split_resp "$(post "$BASE_URL/v1/credentials" "$SOON_JSON")"
expect_code 201 "issue short-lived"
CRED4=$(printf '%s' "$BODY" | jq -r '.credential_id')
EXP=$(printf '%s' "$BODY" | jq -r '.expires_at')
split_resp "$(post "$BASE_URL/v1/verify" \
  "$(jq -n --arg c "$CRED4" --arg t "$EXP" '{credential_id:$c, as_of:$t}')")"
expect_verdict EXPIRED "verify exactly at expires_at (exclusive boundary)"

c_bold "== summary =="
if [[ "$FAILURES" == 0 ]]; then
  c_grn "ALL ACCEPTANCE CHECKS PASSED"
else
  c_red "$FAILURES CHECK(S) FAILED"
  exit 1
fi
