#!/usr/bin/env bash
# End-to-end sample against a running TargetCraft API.
# Usage: BASE=http://localhost:28080 scripts/sample.sh
set -euo pipefail

BASE="${BASE:-http://localhost:28080}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DAY="$(date -u +%Y-%m-%d)"

echo "== create campaign =="
CAMP=$(curl -sS "$BASE/api/v1/campaigns" -H 'Content-Type: application/json' -d "{
  \"name\": \"sample-campaign\",
  \"start_at\": \"${DAY}T00:00:00Z\",
  \"end_at\": \"$(date -u -d '+7 days' +%Y-%m-%dT%H:%M:%SZ)\",
  \"total_budget_cents\": 100000,
  \"daily_cap_cents\": 20000,
  \"regions\": [\"CN\", \"JP\"],
  \"devices\": [\"ios\", \"android\"],
  \"hour_windows\": [\"00:00-23:59\"]
}")
echo "$CAMP" | jq .
CAMPAIGN_ID=$(echo "$CAMP" | jq -r .id)

echo "== create creative =="
CRE=$(curl -sS "$BASE/api/v1/creatives" -H 'Content-Type: application/json' -d "{
  \"campaign_id\": $CAMPAIGN_ID,
  \"name\": \"sample-creative\"
}")
echo "$CRE" | jq .
CREATIVE_ID=$(echo "$CRE" | jq -r .id)

echo "== decide (accepted) =="
curl -sS "$BASE/api/v1/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"sample-req-1\",
  \"creative_id\": $CREATIVE_ID,
  \"user_id\": \"user-1\",
  \"region\": \"CN\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost_cents\": 150
}" | jq .

echo "== decide replay (same body, same response) =="
curl -sS "$BASE/api/v1/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"sample-req-1\",
  \"creative_id\": $CREATIVE_ID,
  \"user_id\": \"user-1\",
  \"region\": \"CN\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost_cents\": 150
}" | jq .

echo "== decide conflict (same id, different cost -> 409) =="
curl -sS -w '\nHTTP %{http_code}\n' "$BASE/api/v1/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"sample-req-1\",
  \"creative_id\": $CREATIVE_ID,
  \"user_id\": \"user-1\",
  \"region\": \"CN\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost_cents\": 999
}" | jq . 2>/dev/null || true

echo "== decide rejected (region not targeted) =="
curl -sS "$BASE/api/v1/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"sample-req-2\",
  \"creative_id\": $CREATIVE_ID,
  \"user_id\": \"user-2\",
  \"region\": \"US\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost_cents\": 150
}" | jq .

echo "== confirm sample-req-1 =="
curl -sS "$BASE/api/v1/decisions/sample-req-1/confirm" -X POST | jq .

echo "== confirm again (idempotent) =="
curl -sS "$BASE/api/v1/decisions/sample-req-1/confirm" -X POST | jq .

echo "== decision record =="
curl -sS "$BASE/api/v1/decisions/sample-req-1" | jq .

echo "== settlements =="
curl -sS "$BASE/api/v1/settlements?campaign_id=$CAMPAIGN_ID" | jq .
