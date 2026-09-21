#!/usr/bin/env bash
# DeskLens end-to-end smoke walkthrough. Requires:
#   docker compose up -d
#
# Every snapshot is one workstation/minute; filtered rows never reach storage.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
ADMIN="admin-dev-key"
WS="ingest-ws101"        # Alice Wang (Engineering, America/New_York)
MGR="mgr-eng"

j() { python3 -m json.tool; }

echo "== 1. Ingest a batch of two minutes =="
curl -s -X POST "$BASE/api/v1/snapshots" \
  -H "X-API-Key: $WS" -H 'Content-Type: application/json' \
  -d '{"batch_id":"3f1a2b90-0001-4000-8000-000000000001","snapshots":[
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:00:00Z","app_name":"Chrome","activity_count":42},
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:01:00Z","app_name":"GoLand","activity_count":18}
  ]}' | j

echo "== 2. Re-send the identical batch: duplicates, processed once =="
curl -s -X POST "$BASE/api/v1/snapshots" \
  -H "X-API-Key: $WS" -H 'Content-Type: application/json' \
  -d '{"batch_id":"3f1a2b90-0001-4000-8000-000000000001","snapshots":[
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:00:00Z","app_name":"Chrome","activity_count":42},
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:01:00Z","app_name":"GoLand","activity_count":18}
  ]}' | j

echo "== 3. Conflicting content for 14:00 -> 409, whole batch rejected =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/snapshots" \
  -H "X-API-Key: $WS" -H 'Content-Type: application/json' \
  -d '{"snapshots":[
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:00:00Z","app_name":"Chrome","activity_count":999}]}'

echo "== 4. Excluded app (1Password) is filtered before storage =="
curl -s -X POST "$BASE/api/v1/snapshots" \
  -H "X-API-Key: $WS" -H 'Content-Type: application/json' \
  -d '{"snapshots":[
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T15:00:00Z","app_name":"1Password 8","activity_count":9}]}' | j

echo "== 5. Exempt department (Customer Success/WS-104) -> filtered =="
curl -s -X POST "$BASE/api/v1/snapshots" \
  -H "X-API-Key: ingest-ws104" -H 'Content-Type: application/json' \
  -d '{"snapshots":[
    {"workstation_id":"WS-104","employee_id":104,"utc_time":"2026-09-15T14:00:00Z","app_name":"Chrome","activity_count":5}]}' | j

echo "== 6. Late backfill for an earlier minute recomputes only that day =="
curl -s -X POST "$BASE/api/v1/snapshots" \
  -H "X-API-Key: $WS" -H 'Content-Type: application/json' \
  -d '{"snapshots":[
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T13:30:00Z","app_name":"Zoom","activity_count":7}]}' | j

echo "== 7. Manager reads the engineering daily summary (department scoped) =="
curl -s "$BASE/api/v1/manager/daily?from=2026-09-14&to=2026-09-20" \
  -H "X-API-Key: $MGR" | j

echo "== 8. Manager reads the engineering weekly summary =="
curl -s "$BASE/api/v1/manager/weekly?from=2026-09-01&to=2026-09-30" \
  -H "X-API-Key: $MGR" | j

echo "== 9. Raw detail rows and CSV export (same department isolation) =="
curl -s "$BASE/api/v1/manager/employees/101/details?from=2026-09-15T00:00:00Z&to=2026-09-16T00:00:00Z" \
  -H "X-API-Key: $MGR" | j
curl -s -D - -o /tmp/desklens-export.csv "$BASE/api/v1/manager/employees/101/export?from=2026-09-15T00:00:00Z&to=2026-09-16T00:00:00Z" \
  -H "X-API-Key: $MGR" | grep -i 'content-type\|content-disposition'
cat /tmp/desklens-export.csv

echo "== 10. Cross-department read -> 404 =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
  "$BASE/api/v1/manager/employees/103/details?from=2026-09-15T00:00:00Z&to=2026-09-16T00:00:00Z" \
  -H "X-API-Key: $MGR"

echo "== 11. Publish classification v2: meetings become unproductive =="
curl -s -X POST "$BASE/api/v1/admin/classification" \
  -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' \
  -d '{"published_by":"walkthrough","rules":[
    {"rule_id":"meet","pattern":"zoom*","category":"unproductive","priority":10},
    {"rule_id":"browser","pattern":"chrome*","category":"productive","priority":20},
    {"rule_id":"code","pattern":"goland*","category":"productive","priority":10}]}' | j

echo "== 12. Rebuild summaries from raw rows; values match incremental output =="
curl -s -X POST "$BASE/api/v1/admin/rebuild" \
  -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' \
  -d '{"start_date":"2026-09-14","end_date":"2026-09-20"}' | j

echo "== 13. Purge old raw data; summaries survive and the boundary is recorded =="
curl -s -X POST "$BASE/api/v1/admin/retention/purge" \
  -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' \
  -d '{"cutoff":"2026-09-15T14:01:00Z","triggered_by":"walkthrough"}' | j
curl -s "$BASE/api/v1/admin/retention" -H "X-API-Key: $ADMIN" | j
