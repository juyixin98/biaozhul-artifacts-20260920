#!/usr/bin/env bash
# 端到端验收脚本：发行 → 验证 → 轮换 → 撤销 → 历史重放 → 缓存失效。
# 前提：服务已在 ${BASE_URL:-http://localhost:8080} 运行，数据库已就绪。
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
DIR="$(cd "$(dirname "$0")" && pwd)"

say()  { printf '\n=== %s ===\n' "$*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# 使用独立签发者，避免与既有数据互相影响。
ISSUER="issuer-accept-$(date +%s)"
PAST="2026-01-01T00:00:00Z"
FUTURE="2027-01-01T00:00:00Z"

say "0. health check"
curl -fsS "$BASE_URL/healthz" | jq .

say "1. create issuer key (rotation #1)"
KEY1=$(curl -fsS -X POST "$BASE_URL/v1/keys" -H 'Content-Type: application/json' \
  -d "{\"issuer\":\"$ISSUER\",\"valid_from\":\"$PAST\"}")
echo "$KEY1" | jq .
KID1=$(echo "$KEY1" | jq -r .kid)

say "2. issue credential"
ISSUE=$(curl -fsS -X POST "$BASE_URL/v1/credentials" -H 'Content-Type: application/json' \
  -d "{\"issuer\":\"$ISSUER\",\"subject\":\"subject-synth-1\",\"purpose\":\"age-check\",\"not_before\":\"$PAST\",\"not_after\":\"$FUTURE\",\"content\":\"synthetic credential content v1\"}")
echo "$ISSUE" | jq .
CRED_ID=$(echo "$ISSUE" | jq -r .credential.id)
[ "$CRED_ID" != "null" ] || fail "no credential id"

say "3. verify (expect valid, snapshot 0)"
V1=$(curl -fsS -X POST "$BASE_URL/v1/verifications" -H 'Content-Type: application/json' \
  -d "{\"credential_id\":\"$CRED_ID\",\"purpose\":\"age-check\"}")
echo "$V1" | jq .
[ "$(echo "$V1" | jq -r .status)" = "valid" ] || fail "expected valid"

say "4. verify with wrong purpose (expect purpose_mismatch)"
V2=$(curl -fsS -X POST "$BASE_URL/v1/verifications" -H 'Content-Type: application/json' \
  -d "{\"credential_id\":\"$CRED_ID\",\"purpose\":\"loan-application\"}")
echo "$V2" | jq .
echo "$V2" | jq -e '.reasons | index("purpose_mismatch")' >/dev/null || fail "expected purpose_mismatch"

say "5. rotate key (rotation #2) and issue a second credential"
KEY2=$(curl -fsS -X POST "$BASE_URL/v1/keys" -H 'Content-Type: application/json' \
  -d "{\"issuer\":\"$ISSUER\"}")
KID2=$(echo "$KEY2" | jq -r .kid)
[ "$KID1" != "$KID2" ] || fail "rotation did not produce a new kid"
ISSUE2=$(curl -fsS -X POST "$BASE_URL/v1/credentials" -H 'Content-Type: application/json' \
  -d "{\"issuer\":\"$ISSUER\",\"subject\":\"subject-synth-2\",\"purpose\":\"age-check\",\"not_before\":\"$PAST\",\"not_after\":\"$FUTURE\",\"content\":\"post-rotation content\"}")
CRED2_ID=$(echo "$ISSUE2" | jq -r .credential.id)
echo "kid1=$KID1 kid2=$KID2 cred1=$CRED_ID cred2=$CRED2_ID"

say "6. both pre- and post-rotation credentials verify (expect valid)"
for CID in "$CRED_ID" "$CRED2_ID"; do
  V=$(curl -fsS -X POST "$BASE_URL/v1/verifications" -H 'Content-Type: application/json' \
    -d "{\"credential_id\":\"$CID\",\"purpose\":\"age-check\"}")
  echo "$V" | jq -c '{credential_id, status, snapshot}'
  [ "$(echo "$V" | jq -r .status)" = "valid" ] || fail "expected valid for $CID"
done

say "7. revoke first credential (returns snapshot)"
REV=$(curl -fsS -X POST "$BASE_URL/v1/credentials/$CRED_ID/revocations" \
  -H 'Content-Type: application/json' -d '{"reason":"subject reported key compromise"}')
echo "$REV" | jq .
SNAP=$(echo "$REV" | jq -r .snapshot)

say "8. verify after revocation (expect revoked, same snapshot, cache refreshed)"
V3=$(curl -fsS -X POST "$BASE_URL/v1/verifications" -H 'Content-Type: application/json' \
  -d "{\"credential_id\":\"$CRED_ID\",\"purpose\":\"age-check\"}")
echo "$V3" | jq .
echo "$V3" | jq -e '.reasons | index("revoked")' >/dev/null || fail "expected revoked"
[ "$(echo "$V3" | jq -r .snapshot)" = "$SNAP" ] || fail "snapshot mismatch: verify=$(echo "$V3" | jq -r .snapshot) revoke=$SNAP"
[ "$(echo "$V3" | jq -r .cache_hit)" = "false" ] || fail "stale cache served after revocation"

say "9. historical verify before the revocation (expect valid again)"
REV_AT=$(echo "$REV" | jq -r .recorded_at)
BEFORE_REV=$(python3 -c "
from datetime import datetime, timedelta, timezone
t = datetime.fromisoformat('$REV_AT'.replace('Z','+00:00')) - timedelta(seconds=2)
print(t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")
V4=$(curl -fsS -X POST "$BASE_URL/v1/verifications" -H 'Content-Type: application/json' \
  -d "{\"credential_id\":\"$CRED_ID\",\"purpose\":\"age-check\",\"at\":\"$BEFORE_REV\"}")
echo "$V4" | jq .
[ "$(echo "$V4" | jq -r .status)" = "valid" ] || fail "historical verify must replay to valid"

say "10. expiry boundary (at == not_after is expired)"
V5=$(curl -fsS -X POST "$BASE_URL/v1/verifications" -H 'Content-Type: application/json' \
  -d "{\"credential_id\":\"$CRED2_ID\",\"purpose\":\"age-check\",\"at\":\"$FUTURE\"}")
echo "$V5" | jq .
echo "$V5" | jq -e '.reasons | index("expired")' >/dev/null || fail "expected expired at not_after"

say "ALL ACCEPTANCE CHECKS PASSED"
