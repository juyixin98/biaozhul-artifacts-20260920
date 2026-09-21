# DeskLens — activity aggregation backend

DeskLens receives **per-minute activity snapshots** from workstation agents and
turns them into employee-daily and department-weekly summaries. There is no
monitoring agent in this repository, and the backend never sees raw keystrokes:
an agent uploads at most one aggregated sample per workstation per minute.

Built with **Go + Echo + sqlx + PostgreSQL**, runnable with a single
`docker compose up`.

## Privacy model

Filtering happens at ingest, *before* the raw table is touched. A snapshot is
dropped (and reported back as `filtered`, never stored) when any of these hold:

1. the employee's department is on the policy's **exempt-department** list;
2. the application matches an **excluded-app** wildcard (e.g. password managers);
3. the bucket minute is **outside the monitoring window** in the employee's own
   time zone.

Filtered data therefore cannot appear in raw storage, summaries, the detail
API, or CSV exports.

## Core guarantees

| Requirement | How it is enforced |
|---|---|
| Idempotent intake | Primary key `(workstation_id, bucket_time)` plus an optional client `batch_id` (UUID). An identical re-send is reported as `duplicate` and processed once. |
| Conflicting re-send rejected | A re-send with different `app_name`/`activity_count` for the same minute returns `409 snapshot_conflict`; the **whole batch** rolls back. |
| Batch validation atomicity | Any invalid item rolls the entire batch back; nothing is partially written. |
| Policy/rule versions preserved | Every raw row stamps the `policy_version` and `classification_version` (and matched `rule_id`) applied to it. Publishing a new version only affects new ingest; summaries record the version span. |
| Stable classification | Rules are evaluated by `priority ASC, rule_id ASC`, first match wins; equal priority is deterministic by rule id. Unmatched apps are `neutral`. |
| Local dates/weeks | Daily buckets are keyed by employee-local date, weekly buckets by the Monday of the employee-local week. Windows, DST and zones are handled in Go with IANA zones. |
| Rebuild = incremental | Both paths call the same `aggregate` recompute functions under per-bucket advisory locks. A rebuild while ingest is running loses no writes and double-counts nothing. |
| Retention boundary | Purging raw data keeps summaries and stores the earliest surviving bucket. Any daily/weekly bucket whose UTC interval intersects purged data is **frozen** at purge time; it is retained as the complete statistic and every recompute path (incremental ingest, explicit rebuild) skips it. A rebuild that would touch a frozen bucket is refused (`422 rebuild_range_purged`). |
| Manager isolation | Every manager read (daily, weekly, detail, export) is scoped to the manager's department; foreign employees return `404`. |

## Quick start

```bash
docker compose up --build
# API on http://localhost:8080, PostgreSQL on localhost:5432
```

The container runs embedded SQL migrations and idempotent reference-data seed on
start. Seed credentials (development only):

| Principal | Key | Scope |
|---|---|---|
| Workstation WS-101..104 | `ingest-ws101` … `ingest-ws104` | its own workstation |
| Engineering manager | `mgr-eng` | department 1 |
| Sales manager | `mgr-sales` | department 2 |
| Finance manager | `mgr-finance` | department 3 |
| Admin | `admin-dev-key` (compose `ADMIN_API_KEY`) | policy/rule publish, rebuild, purge |

Try the guided walkthrough:

```bash
./examples/walkthrough.sh
```

### Local development without Docker

```bash
export DATABASE_URL='postgres://desklens:desklens@localhost:5432/desklens?sslmode=disable'
export ADMIN_API_KEY='admin-dev-key'
go run ./cmd/desklens
```

Migrations are embedded and applied automatically (`RUN_MIGRATIONS=false` to
disable). Reference seed is controlled with `RUN_SEED`.

## API

All endpoints are under `/api/v1`. Authenticate with `X-API-Key: <key>` (or
HTTP Basic `apikey:<key>`).

### Ingest — workstation key

`POST /snapshots`

```json
{
  "batch_id": "3f1a2b90-0001-4000-8000-000000000001",
  "snapshots": [
    {"workstation_id":"WS-101","employee_id":101,"utc_time":"2026-09-15T14:00:00Z",
     "app_name":"Chrome","activity_count":42}
  ]
}
```

* `batch_id` is optional (server generates one); supply it to make retries
  idempotent — replaying a completed batch returns the stored outcome.
* `utc_time` is truncated to the minute; future times are rejected.
* Response lists every item as `accepted`, `duplicate` (with reason) or
  `filtered` (`exempt_department` / `excluded_app` / `outside_window`).

Errors: `400 invalid_item|duplicate_in_batch|invalid_batch_id`,
`403 workstation_mismatch`, `409 snapshot_conflict`,
`422 beyond_retention_boundary`, `503 no_policy|no_classification`.

### Manager reads — manager key

* `GET /manager/daily?from=YYYY-MM-DD&to=YYYY-MM-DD[&employee_id=N]`
* `GET /manager/weekly?from=YYYY-MM-DD&to=YYYY-MM-DD`
* `GET /manager/employees/:id/details?from=<RFC3339>&to=<RFC3339>` — stored raw
  rows (excluded/exempt rows never exist here)
* `GET /manager/employees/:id/export?from=<RFC3339>&to=<RFC3339>` — same data as
  CSV with the identical department filter

Dates on daily/weekly endpoints are compared against employee-local bucket
dates; detail/export intervals are UTC.

### Admin — admin key

* `POST /admin/policy` — publish a new immutable policy (see
  `examples/publish_policy.json`). `monitoring_end` may be `24:00` for an
  all-day window; `start == end` is rejected.
* `POST /admin/classification` — publish a new immutable rule set
  (`examples/publish_classification.json`).
* `POST /admin/rebuild` — recompute summaries for a local-date range from raw
  rows (`examples/rebuild.json`). Buckets that are not frozen are rebuilt; the
  request is refused with `422 rebuild_range_purged` if any targeted daily or
  weekly bucket is frozen.
* `POST /admin/retention/purge` — delete raw rows older than an RFC3339 cutoff;
  summaries survive, affected daily/weekly buckets are frozen, and the rebuild
  boundary advances (never backward).
* `GET /admin/retention` — current earliest surviving snapshot and cleanup
  event history.

### Frozen summaries after a purge

Freezing is what makes "keep summaries after raw-data cleanup" sound. At purge
time the backend marks:

* every employee-local day whose UTC interval could contain deleted buckets;
* every department-local week that includes a frozen day or a deleted raw row.

Frozen rows are never deleted or overwritten by the delete-then-reinsert
recomputation. Incremental ingest of new, post-boundary snapshots still updates
unfrozen days/weeks normally; a mid-purge-week frozen weekly total stays at the
last complete value while the surviving days in that week keep updating.

## Storage layout

* `activity_snapshots` — one row per workstation/minute with stamped versions;
  filtered data never reaches it.
* `daily_employee_summary` — per employee / local date, category sums of
  `activity_count` with policy/classification version min/max and a `frozen`
  marker.
* `weekly_department_summary` — per department / local week (Monday), with a
  `frozen` marker.
* `policy_versions` (+ excluded apps, exempt departments) and
  `classification_versions` (+ rules) are append-only.
* `retention_state`, `cleanup_events` — the rebuild boundary and purge audit.
* `ingest_batches` — committed/rejected batch audit trail.

## Tests

```bash
# Unit tests (no database):
go test ./internal/...

# Full integration suite against PostgreSQL (schema-isolated per test):
TEST_DATABASE_URL='postgres://desklens:desklens@localhost:5432/desklens_test?sslmode=disable' \
  go test -race ./...
```

Coverage maps to the required scenarios:

* **privacy filtering** — exempt department, excluded app, outside window, and
  that filtered rows are absent from detail/export (`TestPrivacyFiltering`);
* **cross-day/cross-week** — Shanghai/Berlin/NY zones around midnight and week
  boundaries (`TestCrossDayAndWeek`);
* **late backfill** — only the affected local date/week is recomputed
  (`TestLateArrivals`, `TestRebuildLateDate`);
* **idempotency/conflict/rollback** — duplicates, conflicts, invalid items,
  batch replay (`TestIdempotencyAndConflict`, HTTP equivalents);
* **rule & policy versions** — history keeps its version/category, new ingest
  uses the new version (`TestClassificationVersioning`, `TestPolicyVersioning`);
* **rebuild concurrency** — 60 concurrent ingests vs 8 concurrent rebuilders,
  exact-total assertion under `-race` (`TestRebuildConcurrency`);
* **rebuild parity** — wipe summaries, rebuild, byte-level equality with
  incremental output (`TestRebuildMatchesIncremental`);
* **cleanup boundary** — summaries survive purges, affected buckets are
  frozen, a late row in a mid-purge week cannot overwrite the complete weekly
  total with partial raw data, partial-history rebuilds are refused, boundary
  monotonic (`TestPurgeBoundary`, `TestPurgeLateRowSafety`,
  `TestPurgeEntireEmployee`);
* **manager isolation** — daily/weekly/detail/export scoping and 404s
  (`TestManagerIsolation`, `TestExemptDepartmentNoRaw`).

## Project layout

```
cmd/desklens/          service entrypoint (migrate, seed, serve)
internal/db/           connection, embedded migrations, seed
internal/model/        domain types
internal/pattern/      local wildcard matcher (* and ?, case-insensitive)
internal/timeutil/     monitoring windows, local dates, local weeks
internal/store/        current policy/classification resolution
internal/ingest/       snapshot intake, filtering, idempotency, conflicts
internal/aggregate/    shared summary recomputation + advisory locks
internal/admin/        policy/classification publish, rebuild, purge
internal/manager/      department-scoped reads and CSV export
internal/auth/         workstation / manager / admin API-key middleware
internal/api/          Echo routing and JSON/CSV handlers
integration/           PostgreSQL-backed end-to-end tests
examples/              sample payloads and a curl walkthrough
```
