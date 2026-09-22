# AnomalyWatch — Employee Activity Anomaly Detection (backend)

Local-only backend that ingests employee activity events (logins, file
downloads, USB events) and detects anomalous behavior. Built with **Go, Gin,
GORM, MySQL and Docker Compose**. No data leaves the host: the only external
dependency is the local MySQL container.

## What it detects

| Rule | Code | Default definition |
|---|---|---|
| Download burst | `download_burst` | **More than 50** file downloads within any **10-minute** window (fixed UTC tumbling buckets) |
| First USB use | `first_usb` | The first USB event ever seen for an employee (severity high) |
| Night activity | `night_activity` | Any event between **20:00 and 06:00 local time**, using each employee's configured IANA time zone |
| Statistical volume | `statistical` | A completed local day whose event count is **more than 2.5 standard deviations** above the prior **30 days**; with fewer than **10 sampled days** the statistical rule stays silent and only fixed rules apply |

Rules are stored versioned. Editing a rule bumps its version, writes an audit
record, and causes affected windows to be re-evaluated on the next sweep; every
alert permanently keeps the rule version and evidence that fired it.

## Key guarantees

- **Batch intake, max 2000 events/request**, each with event ID, employee,
  occurrence time and arbitrary JSON metadata.
- **Idempotent**: identical duplicate reports are accepted (`duplicate`) but
  booked exactly once, enforced by a unique `(source, event_id)` index plus
  `INSERT IGNORE` for concurrent writers.
- **Conflict detection**: same ID with different content returns **HTTP 409**
  and never overwrites the stored row; other valid rows in the batch still
  commit.
- **Out-of-order / late events** are accepted as long as they fall inside the
  explicitly allowed backfill window (default: last **30 days**,
  `MAX_BACKFILL_AGE`; up to 5 minutes of clock skew into the future). Every
  affected window is recomputed.
- **No duplicate alerts**: alerts carry a stable dedup key
  `(rule, version, employee, window)`; recomputation refreshes evidence
  in place. A window that no longer qualifies after backfill has its open alert
  withdrawn (resolved/false-positive outcomes are kept as history).
- **Alert lifecycle**: `new → investigating → escalated → resolved |
  false_positive` (plus direct jumps). Terminal states are immutable.
  **`new` alerts older than 24h are auto-escalated** by the scheduler.
- **RBAC**: analysts see only their assigned department (events, alerts,
  employees); admins manage rules and employee time zones and read the audit
  log. Investigations (status changes), rule edits and time-zone changes are
  all audited.
- **Restart-safe / repeatable scheduler**: unprocessed events are claimed from
  the database (`events.processed`), jobs are guarded by MySQL named locks, and
  a periodic sweep is a backstop that evaluates every window in the backfill
  horizon missing at the current rule version. Duplicate ticks, restarts and
  multiple replicas never double-book or double-alert.
- Time boundaries use real IANA zones, including DST transitions and the
  cross-midnight ownership of night windows.
- **No performance scoring** of individuals exists anywhere in the system.

## Quick start

Requirements: Docker + Docker Compose.

```bash
docker compose up -d --build
```

- API: http://localhost:18023 (host port `18023` → container 8080)
- MySQL: localhost:13323 (user `anomaly`, password `anomaly`, db `anomalywatch`)

Migrations run automatically at startup; reference data (2 departments,
4 employees across 4 time zones, 4 rules, 2 API keys) is seeded idempotently.

Default API keys (override with `ADMIN_API_KEY` / `ANALYST_API_KEY`):

| Key | Role | Scope |
|---|---|---|
| `admin-local-key` | admin | everything |
| `analyst-local-key` | analyst | Engineering department only |

Health check:

```bash
curl -s http://localhost:18023/health -H "X-API-Key: admin-local-key"
```

## Try it with sample events

The repository includes `samples/events.json` (57 events): a 51-download burst
for Alice (Shanghai, 20:00 local boundary), Bob's first USB at New York night
time, Carol's London 05:59/06:00 boundary pair, a normal daytime event, and a
late out-of-order download.

Run the importer against the live stack:

```bash
# from the host (binary)
go run ./cmd/importer --url http://localhost:18023 \
  --key admin-local-key --file samples/events.json

# or inside the api container
docker compose exec api /app/importer --url http://127.0.0.1:8080 \
  --key admin-local-key --file /app/samples/events.json
```

The processing worker runs every 5 s; the statistical rule is evaluated by the
hourly sweep (completed local days only). Jobs can also be triggered
immediately (admin only):

```bash
curl -s -X POST http://localhost:18023/admin/jobs/process \
  -H "X-API-Key: admin-local-key"
curl -s -X POST http://localhost:18023/admin/jobs/sweep \
  -H "X-API-Key: admin-local-key"
curl -s -X POST http://localhost:18023/admin/jobs/escalate \
  -H "X-API-Key: admin-local-key"
```

Inspect results:

```bash
curl -s "http://localhost:18023/api/v1/alerts?limit=50" \
  -H "X-API-Key: admin-local-key" | jq
```

A generated 30-day statistical spike scenario (29 quiet days with five
working-hours logins each, then a 40-event spike day — pass the employee's
zone so the baseline stays in working hours):

```bash
go run ./cmd/importer --scenario stat-spike --employee-id 3 --emp-tz Europe/London
curl -s -X POST http://localhost:18023/admin/jobs/sweep -H "X-API-Key: admin-local-key"
curl -s "http://localhost:18023/api/v1/alerts?rule_code=statistical" \
  -H "X-API-Key: admin-local-key" | jq
```

## HTTP API

All routes require header `X-API-Key: <key>`. JSON in/out, times RFC 3339 UTC.

### Events

`POST /api/v1/events/batch` — upload a batch (max 2000).

```json
{
  "source": "edr-agent",
  "events": [
    {
      "event_id": "evt-0001",
      "employee_id": 1,
      "event_type": "file_download",
      "occurred_at": "2026-09-20T12:00:05Z",
      "metadata": {"file": "report.xlsx", "size_bytes": 1234}
    }
  ]
}
```

`event_type` ∈ `login | file_download | usb`. Responses:

- `202` — per-item statuses `inserted | duplicate`;
- `409` — one or more `conflict` items (same ID, different content); valid
  items still commit;
- `400` — whole batch rejected (oversized, bad enum, unknown employee, event
  outside the backfill window, duplicate ID within the batch).

`GET /api/v1/events` — paged (`limit`, `offset`), filters `employee_id`,
`event_type`, `from`, `to`. Analysts are restricted to their department.

### Alerts

- `GET /api/v1/alerts` — filters `status`, `employee_id`, `rule_code`; paged;
  department-scoped for analysts.
- `GET /api/v1/alerts/{id}` — full alert incl. `evidence`, `rule_version`,
  window bounds and lifecycle timestamps.
- `POST /api/v1/alerts/{id}/transition` — body `{"status":"investigating",
  "note":"..."}`; audited; `409` for illegal/terminal transitions.

### Admin (admin role only)

- `GET /api/v1/rules`, `PUT /api/v1/rules/{code}` — params by rule:
  - `download_burst`: `window_minutes` (1..1440), `threshold` (>0)
  - `night_activity`: `start_hour`, `end_hour` (0..23, different)
  - `statistical`: `history_days` (2..365), `z_score` (>0),
    `min_samples` (2..history_days)
  - also editable: `name`, `description`, `enabled`
- `PUT /api/v1/employees/{id}/timezone` — body `{"time_zone":"Asia/Shanghai"}`;
  invalidates night/stat window evaluations so the next sweep recomputes them.
- `GET /api/v1/departments`, `GET /api/v1/employees`
- `GET /api/v1/audit?action=rule.update&entity_type=rule`
- `POST /admin/jobs/{process,sweep,escalate}` — manual job triggers.

## How detection is scheduled

1. New events are stored with `processed=0`.
2. The **process** job (default every 5 s) claims batches
   (`UPDATE … WHERE processed=0`) and evaluates exactly the windows the claimed
   events belong to (burst bucket, owning night, employee's first-USB marker,
   that local day's statistics if already complete).
3. The **sweep** job (default hourly) enumerates every window in the 30-day
   backfill horizon that lacks an evaluation at the current rule version and
   evaluates it — this covers late backfills, rule edits, time-zone edits and
   any window missed because of a crash.
4. The **escalate** job (default every minute) flips `new` alerts older than
   24 h to `escalated` with a system audit row.

All four fixed/statistical checks are idempotent: unique window keys,
upserted alerts, MySQL named locks (`job:sweep`, `job:escalate`) around the
cross-employee jobs.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `HTTP_ADDR` | `:8080` | listen address |
| `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME` | `127.0.0.1`/`3306`/`anomaly`/`anomaly`/`anomalywatch` | MySQL coordinates |
| `MAX_EVENTS_PER_BATCH` | `2000` | batch cap |
| `MAX_BACKFILL_AGE` | `720h` (30d) | allowed late-reporting horizon |
| `MAX_FUTURE_DELAY` | `5m` | allowed clock skew into the future |
| `BURST_WINDOW` | `10m` | burst window width (rule-seeded default) |
| `BURST_THRESHOLD` | `50` | strictly-greater-than threshold |
| `NIGHT_START_HOUR` / `NIGHT_END_HOUR` | `20` / `6` | local night window (wraps midnight) |
| `STAT_HISTORY_DAYS` / `STAT_ZSCORE` / `STAT_MIN_SAMPLES` | `30` / `2.5` / `10` | statistical rule |
| `ESCALATION_AGE` | `24h` | unacknowledged alert auto-escalation age |
| `PROCESS_INTERVAL` / `SWEEP_INTERVAL` / `ESCALATE_INTERVAL` | `5s` / `1h` / `1m` | scheduler periods |
| `ADMIN_API_KEY` / `ANALYST_API_KEY` | `admin-local-key` / `analyst-local-key` | seeded keys |

## Running locally without Docker

```bash
# point at any MySQL 8+ and run
go run ./cmd/server --migrations ./migrations
```

## Tests

Pure unit tests run anywhere; integration tests need MySQL and create/drop a
uniquely named database per test:

```bash
# unit tests
go test ./...

# integration tests against the compose MySQL (once it is up)
TEST_MYSQL_DSN='root:rootlocal@tcp(127.0.0.1:13323)/' go test ./...
```

Coverage highlights (see `internal/*/[a-z_]*_test.go`):

- **concurrent import**: 12 identical batches + 8×50 disjoint concurrent rows,
  asserting no false conflicts, no duplicate rows and no deadlocks;
- **out-of-order backfill**: late events recompute windows and refresh
  evidence without duplicating alerts; a late *earlier* USB re-anchors the
  first-USB alert;
- **cross-day / timezone boundaries**: Shanghai/New York/London edges at
  20:00/06:00, ownership by the previous local calendar day after midnight,
  and the US fall-back DST night (11 UTC hours);
- **duplicate scheduling**: MySQL named locks + idempotent escalation;
  exclusive event claiming; sweep idempotency;
- **department access control**: analyst isolation, 404 on cross-department
  alert access/transition, 403 on admin endpoints;
- **statistics**: spike fires above z=2.5 (incl. zero-variance baseline),
  insufficient samples stay silent, normal day stays silent;
- alert state machine, 24 h auto-escalation, rule versioning and time-zone
  invalidation with audit rows.

## Project layout

```
cmd/server        HTTP API + scheduler entrypoint
cmd/importer      sample/scenario event uploader
migrations        SQL migrations (tracked in schema_migrations)
samples           sample event batch
internal/config   env configuration
internal/database connection, DB bootstrap, migration runner
internal/models   GORM models / domain constants
internal/timeutil timezone-aware window bucketing (unit-tested)
internal/ingest   batch intake, idempotency/conflict/backfill rules
internal/detection window engine, fixed + statistical rules, sweep/escalation jobs
internal/alerts   status state machine + audit
internal/admin    rule versioning, time-zone maintenance
internal/httpapi  Gin routes, API-key auth, RBAC
internal/scheduler periodic jobs
internal/seed     default rules/departments/employees/keys
internal/testdb   disposable-MySQL integration-test harness
```
