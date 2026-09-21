# DeskLens API examples

All commands assume the server is at http://localhost:8080.
Tokens come from `0002_seed.sql` (replace in production).

## 1. Send one minute-batch of activity snapshots

Agents post per-minute snapshots (no keystrokes are ever collected).

```bash
curl -s localhost:8080/api/v1/snapshots \
  -H 'Content-Type: application/json' \
  --data @examples/snapshots.json | jq
```

Expected shape — note 1Password is excluded and the Legal workstation is in an
exempt department, so neither reaches storage:

```json
{
  "received": 8,
  "accepted": 6,
  "duplicate": 0,
  "filtered": {"exempt_department": 1, "excluded_app": 1, "outside_window": 0}
}
```

Posting the identical file again returns `accepted: 0, duplicate: 6`.

## 2. Conflicting repeat is refused

Send a known key with a different count — the whole batch is rejected with
HTTP 409 `conflicting_idempotency_key` and nothing is written:

```bash
curl -i localhost:8080/api/v1/snapshots \
  -H 'Content-Type: application/json' \
  -d '{"snapshots":[{"workstation_id":1001,"employee_id":101,"minute_utc":"2026-03-10T09:00:00Z","app_name":"VS Code","activity_count":7}]}'
```

## 3. Read daily summaries (manager, department-scoped)

```bash
curl -s localhost:8080/api/v1/summaries/daily \
  -H 'Authorization: Bearer manager-eng-token' | jq
```

The engineering manager never sees Sales rows; asking for
`?department_id=2` returns HTTP 403.

## 4. Read weekly summaries / detail / CSV export

```bash
curl -s "localhost:8080/api/v1/summaries/weekly" \
  -H 'Authorization: Bearer admin-token' | jq

curl -s "localhost:8080/api/v1/activity?from=2026-03-10T00:00:00Z&to=2026-03-11T00:00:00Z" \
  -H 'Authorization: Bearer manager-eng-token' | jq

curl -s "localhost:8080/api/v1/export/activity.csv" \
  -H 'Authorization: Bearer manager-eng-token'
```

## 5. Late snapshot and rebuild

A snapshot arriving days late only recomputes the affected employee-day and
department-week (they use the same write path as live ingestion). An explicit
rebuild from raw data yields identical numbers:

```bash
curl -s -X POST localhost:8080/api/v1/rebuild \
  -H 'Authorization: Bearer admin-token' \
  -H 'Content-Type: application/json' -d '{"scope":"all"}' | jq
```

## 6. Publish a new policy / classification version

New versions only affect ingestion after publication; history keeps the
versions stamped on every raw row.

```bash
curl -s -X POST localhost:8080/api/v1/admin/policy \
  -H 'Authorization: Bearer admin-token' -H 'Content-Type: application/json' \
  -d '{"window_start_minute":540,"window_end_minute":1080,
       "excluded_apps":["1password*","*vault*"],"exempt_departments":[3]}'

curl -s -X POST localhost:8080/api/v1/admin/classification \
  -H 'Authorization: Bearer admin-token' -H 'Content-Type: application/json' \
  -d '{"rules":[{"rule_id":1,"pattern":"*terminal*","category":"productive","priority":100},
                 {"rule_id":2,"pattern":"*","category":"neutral","priority":0}]}'
```

## 7. Purge old raw data (summaries survive)

```bash
curl -s -X POST localhost:8080/api/v1/admin/retention/purge \
  -H 'Authorization: Bearer admin-token' -H 'Content-Type: application/json' \
  -d '{"older_than_utc":"2026-03-11T00:00:00Z"}'

curl -s localhost:8080/api/v1/admin/retention \
  -H 'Authorization: Bearer admin-token' | jq
```

After purging, rebuilding those days returns HTTP 409 — partial raw coverage
cannot overwrite the retained complete statistics.
