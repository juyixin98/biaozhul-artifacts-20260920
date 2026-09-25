#!/usr/bin/env bash
# Manual end-to-end walkthrough using curl. Run against a freshly started
# server:
#
#   go run ./cmd/server -addr 127.0.0.1:28080 -db ./data/demo.json
#   bash examples/requests.sh
#
# Every line is a real request; jq pretty-prints the response.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:28080}"
j() { jq .; }
# compact view of one ingest result, tolerating an omitted events array
view() { jq '.data.results[0] | {ts, value, duplicate, overwrote, late, accepted, events: [(.events // [])[].type]}'; }

echo "### 0. health / clock"
curl -s "$BASE/health" | j
curl -s "$BASE/clock" | j

echo "### 1. create a rule: fire when cpu >= 80 for 60s, recover only after"
echo "###    90s below 70 (value band); no data for 2 minutes => nodata"
curl -s -X POST "$BASE/rules" -H 'Content-Type: application/json' -d '{
  "id": "cpu-demo",
  "metric": "cpu_usage",
  "operator": ">=",
  "threshold": 80,
  "trigger_for": "60s",
  "recover_for": "90s",
  "no_data_for": "2m",
  "recovery_threshold": 70
}' | j

echo "### 2. jitter: hot/warm alternating, no hot streak reaches 60s"
T0=1767225600            # arbitrary virtual start (Unix seconds, 2026-01-01)
post_sample() { # ts value
  curl -s -X POST "$BASE/samples" -H 'Content-Type: application/json' \
    -d "{\"samples\":[{\"metric\":\"cpu_usage\",\"ts\":$1,\"value\":$2}]}"
}
post_sample $((T0+20)) 90  | view
post_sample $((T0+40)) 75  | view
post_sample $((T0+60)) 90  | view
post_sample $((T0+80)) 75  | view

echo "### 3. sustained hot streak at +100,+130,+160 reaches 60s -> ONE firing"
post_sample $((T0+100)) 90 | view
post_sample $((T0+130)) 91 | view
post_sample $((T0+160)) 92 | view

echo "### 4. warm-zone value 76 (70..80) keeps the alert open, no recovery"
post_sample $((T0+180)) 76 | view

echo "### 5. cold values (<70) must sustain 90s: +200,+250,+290 -> resolved"
post_sample $((T0+200)) 40 | view
post_sample $((T0+250)) 40 | view
post_sample $((T0+290)) 40 | view

echo "### 6. duplicate at an existing timestamp: no duration, no new event"
post_sample $((T0+290)) 40 | view

echo "### 7. late (out-of-order) sample: stored+queryable, never evaluated"
post_sample $((T0+10)) 999 | view

echo "### 8. advance the virtual clock 5 minutes without samples -> nodata"
curl -s -X POST "$BASE/clock/tick" -H 'Content-Type: application/json' \
  -d '{"duration":"5m"}' | jq '.data | {clock_now, events: [.events[] | {type, from, to}]}'

echo "### 9. resume with healthy data -> data_resumed"
NOW=$(curl -s "$BASE/clock" | jq -r '.data.now')
NEXT=$(date -u -d "$NOW + 30 seconds" +%s)
post_sample "$NEXT" 30 | view

echo "### 10. update the rule -> version bump, explicit state reset + audit"
curl -s -X PUT "$BASE/rules/cpu-demo" -H 'Content-Type: application/json' -d '{
  "metric": "cpu_usage", "operator": ">", "threshold": 95,
  "trigger_for": "30s", "recover_for": "30s", "no_data_for": "2m"
}' | jq '.data | {version: .rule.version, state: .state.state, threshold: .rule.threshold}'

echo "### 11. query state, notifications, event-type counts and raw samples"
curl -s "$BASE/states" | j
curl -s "$BASE/events" | jq '.data.events[] | {ts, type, from, to, message}'
curl -s "$BASE/events?all=true" | jq '[.data.events[] | .type] | group_by(.) | map({(.[0]): length}) | add'
curl -s "$BASE/samples/cpu_usage?limit=3" | jq '.data.samples'

echo "### 12. cleanup: delete the rule (raw samples are retained)"
curl -s -X DELETE "$BASE/rules/cpu-demo" | j
