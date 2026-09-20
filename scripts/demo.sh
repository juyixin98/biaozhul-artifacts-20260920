#!/usr/bin/env bash
# Guided ConsentVault demo against a running API (default http://localhost:8000).
# Usage: ./scripts/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ADMIN="Authorization: Bearer demo-admin-key"
AUDITOR="Authorization: Bearer demo-auditor-key"
ORG2="Authorization: Bearer demo-org2-admin-key"
CT="Content-Type: application/json"

say() { printf "\n\033[1;36m== %s ==\033[0m\n" "$1"; }
req() { curl -s -H "$CT" "$@"; }

say "health"
curl -s "$BASE/health"; echo

say "policies: publish a new immutable version"
POLICY_V=$(req -X POST "$BASE/policies" -H "$ADMIN" -d '{"body":"Privacy policy, demo-published terms"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')
echo "published policy version: $POLICY_V"

say "grant consent for user-1 / analytics (explicitly pins policy $POLICY_V)"
req -X POST "$BASE/consents" -H "$ADMIN" -d "{
  \"event_id\":\"demo-grant-1\",\"subject_key\":\"user-1\",\"purpose\":\"analytics\",
  \"action\":\"grant\",\"expected_version\":0,\"policy_version\":$POLICY_V,
  \"expires_at\":\"2030-01-01T00:00:00+00:00\"}"; echo

say "verify current validity (auditor token, read-only)"
curl -s -H "$AUDITOR" "$BASE/subjects/user-1/consents/analytics"; echo

say "publish a newer policy (does NOT rebind the standing grant)"
NEW_V=$(req -X POST "$BASE/policies" -H "$ADMIN" -d '{"body":"Privacy policy, newer terms"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])')
echo "newer policy version: $NEW_V"
curl -s -H "$AUDITOR" "$BASE/subjects/user-1/consents/analytics" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('grant still pinned to policy_version:',d['policy_version'],'(newest is $NEW_V)')"

say "withdraw, then replay the old grant id (must not restore consent)"
req -X POST "$BASE/consents" -H "$ADMIN" -d '{
  "event_id":"demo-withdraw-1","subject_key":"user-1","purpose":"analytics",
  "action":"withdraw","expected_version":1}'; echo
req -X POST "$BASE/consents" -H "$ADMIN" -d "{
  \"event_id\":\"demo-grant-1\",\"subject_key\":\"user-1\",\"purpose\":\"analytics\",
  \"action\":\"grant\",\"expected_version\":0,\"policy_version\":$POLICY_V,
  \"expires_at\":\"2030-01-01T00:00:00+00:00\"}" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("replay replayed =",d["replayed"])'
curl -s -H "$AUDITOR" "$BASE/subjects/user-1/consents/analytics" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("valid now:",d["valid"],"-",d["reason"])'

say "idempotency: same id, different payload -> 409"
req -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/consents" -H "$ADMIN" -d '{
  "event_id":"demo-grant-1","subject_key":"user-1","purpose":"marketing",
  "action":"grant","expected_version":0}'

say "batch import (max 500), then identical re-import returns originals"
BATCH='{"events":[
 {"event_id":"b1","subject_key":"u-a","purpose":"analytics","action":"grant","expected_version":0},
 {"event_id":"b2","subject_key":"u-b","purpose":"analytics","action":"grant","expected_version":0}]}'
req -X POST "$BASE/admin/import" -H "$ADMIN" -d "$BATCH" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("first:  accepted",d["accepted"],"replayed",d["replayed"])'
req -X POST "$BASE/admin/import" -H "$ADMIN" -d "$BATCH" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("repeat: accepted",d["accepted"],"replayed",d["replayed"])'

say "rebuild derived state from immutable history"
req -X POST "$BASE/admin/rebuild" -H "$ADMIN"; echo

say "erase user u-a: exports & mapping removed, audit stays personal-data-free"
req -X POST "$BASE/subjects/u-a/exports" -H "$ADMIN" -d '{"label":"dsar","payload":{"email":"a@example.com"}}' >/dev/null
req -X POST "$BASE/subjects/u-a/erase" -H "$ADMIN"; echo
curl -s -o /dev/null -w "query after erase -> HTTP %{http_code}\n" -H "$AUDITOR" "$BASE/subjects/u-a/consents/analytics"
req -X POST "$BASE/admin/rebuild" -H "$ADMIN" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("rebuild materialized states:",d["materialized_states"])'

say "auditor cannot write (403); org2 cannot read org1 subject (404)"
req -o /dev/null -w "auditor publish -> HTTP %{http_code}\n" -X POST "$BASE/policies" -H "$AUDITOR" -d '{"body":"x"}'
curl -s -o /dev/null -w "org2 read of user-1 -> HTTP %{http_code}\n" -H "$ORG2" "$BASE/subjects/user-1/consents/analytics"

say "OpenAPI docs are available at $BASE/docs"
