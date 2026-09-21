#!/usr/bin/env bash
# End-to-end demo against a running TargetCraft server (default :8080).
# Assumes SEED_SAMPLE_DATA=true so campaign 1 with creatives 1,2 exists.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== health =="
curl -s "$BASE/api/health" | jq .

echo "== create campaign (or use seeded campaign 1) =="
curl -s -X POST "$BASE/api/campaigns" -H 'Content-Type: application/json' -d "{
  \"name\": \"demo-campaign\",
  \"start_at\": \"$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)\",
  \"end_at\":   \"$(date -u -d '+7 day'  +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v+7d +%Y-%m-%dT%H:%M:%SZ)\",
  \"total_budget\": 100000,
  \"daily_cap\": 10000,
  \"rules\": {\"regions\": [\"CN\",\"US\"], \"devices\": [\"ios\",\"android\"], \"hours\": []}
}" | jq .

echo "== create creative for campaign 1 =="
curl -s -X POST "$BASE/api/campaigns/1/creatives" -H 'Content-Type: application/json' \
  -d '{"name": "demo-banner", "content": "Hello TargetCraft"}' | jq .

echo "== decision request (approved) =="
curl -s -X POST "$BASE/api/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"demo-req-1\",
  \"user_id\": \"user-42\",
  \"campaign_id\": 1,
  \"region\": \"CN\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost\": 150
}" | jq .

echo "== idempotent replay (same request_id, same payload) =="
curl -s -X POST "$BASE/api/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"demo-req-1\",
  \"user_id\": \"user-42\",
  \"campaign_id\": 1,
  \"region\": \"CN\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost\": 150
}" | jq .

echo "== conflict (same request_id, different cost) -> HTTP 409 =="
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/decisions" -H 'Content-Type: application/json' -d "{
  \"request_id\": \"demo-req-1\",
  \"user_id\": \"user-42\",
  \"campaign_id\": 1,
  \"region\": \"CN\",
  \"device\": \"ios\",
  \"occurred_at\": \"$NOW\",
  \"cost\": 999
}"

echo "== confirm impression =="
curl -s -X POST "$BASE/api/decisions/demo-req-1/confirm" | jq .

echo "== decisions & settlements =="
curl -s "$BASE/api/decisions?campaign_id=1" | jq '.decisions | length'
curl -s "$BASE/api/settlements?campaign_id=1" | jq .
