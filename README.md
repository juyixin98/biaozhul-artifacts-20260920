# DeskLens — activity aggregation backend

DeskLens receives **per-minute activity snapshots** from workstation agents and
turns them into employee-daily and department-weekly summaries.

It deliberately does **not** run an agent and never ingests raw keystrokes.
Each snapshot is just `{workstation, employee, minute (UTC), app name,
activity count}`.

## Quick start (Docker)

```bash
docker compose up -d --build
# API at http://localhost:8080, Postgres at localhost:5432
curl localhost:8080/healthz
```

Migrations (embedded into the binary) run automatically on boot and are
recorded in `schema_migrations`. Local development without Docker:

```bash
docker compose up -d postgres
make run          # DATABASE_URL defaults to localhost:5432
```

## API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/snapshots` | open (agents) | one batch of minute snapshots |
| GET  | `/api/v1/summaries/daily` | token | employee-daily summaries |
| GET  | `/api/v1/summaries/weekly` | token | department-weekly summaries |
| GET  | `/api/v1/activity` | token | raw detail (scoped) |
| GET  | `/api/v1/export/activity.csv` | token | CSV export (same scope) |
| POST | `/api/v1/rebuild` | admin | rebuild day / week / all from raw |
| POST | `/api/v1/admin/retention/purge` | admin | delete old raw data |
| GET  | `/api/v1/admin/retention` | admin | purge ledger + rebuildable window |
| POST | `/api/v1/admin/policy` | admin | publish a new policy version |
| POST | `/api/v1/admin/classification` | admin | publish a new rule-set version |

Worked curl examples: [`examples/README.md`](examples/README.md).
Demo tokens (seed data): `admin-token`, `manager-eng-token`,
`manager-sales-token`.

## Design guarantees

### Idempotent, conflict-aware ingestion
* Natural key is `(workstation_id, minute_utc, app_name)`; minute is truncated
  to the whole minute in UTC.
* An identical repeat is counted as `duplicate` and never processed twice.
* A repeat with a **different** `activity_count` is a hard `409 conflict` and
  the **entire batch rolls back** — conflict data is never overwritten or
  partially stored. All structural validation likewise rolls back the batch.

### Privacy filtering before storage
The current policy filters inside the ingest transaction **before any insert**,
so excluded apps and exempt departments never reach the raw table or any
statistic:
* exempt departments (`policy_exempt_departments`),
* excluded application globs (`policy_excluded_apps`, case-insensitive `*`
  wildcards),
* minutes outside the employee's monitoring window, evaluated in the
  employee's own IANA timezone (overnight windows supported).

Filtered rows are acknowledged in the response
(`filtered: {exempt_department, excluded_app, outside_window}`).

### Versioned policy and classification
* Policies and rule sets are immutable, append-only versions.
* Publishing only affects **new** ingestion. Every raw snapshot records the
  exact `policy_version` and `classif_version` applied; summaries carry
  version stamps too, so a rule change can never silently recolor history.
* App classification uses local glob patterns →
  productive / non_productive / neutral. Highest priority wins; equal
  priorities resolve to the lowest `rule_id` (deterministic, stable).

### Timezone-correct days and weeks
* `local_date` is the calendar date in the **employee's** timezone, so UTC
  minutes around midnight land on the correct local day.
* The ISO week (Monday) is derived from `local_date` at aggregation time, so
  late-evening UTC minutes bucket into the department week they locally belong
  to.

### Summaries: one formula, two paths
* Daily and weekly summaries are always produced by a full
  `SUM … GROUP BY` recompute over raw rows — the incremental path (ingest) and
  a full rebuild call the same SQL, so their results are identical by
  construction.
* Each recompute takes a **per-key transaction-scoped advisory lock** (keys
  acquired in deterministic order to avoid deadlocks). A late snapshot only
  recomputes the affected employee-day and the affected department-week.
* Because recompute runs in the same transaction as the insert under that
  lock, rebuilds running concurrently with new writes cannot lose or
  double-count rows (verified by a race-detector concurrency test).

### Retention and rebuild safety
* Purging deletes raw rows but **keeps summaries**. Every touched
  employee/day is marked `partial` with the count of surviving minutes, and a
  `raw_retention_runs` ledger entry records the cutoff;
  `/admin/retention` reports the window where raw data still exists.
* Rebuilding a partially-covered day or week returns `409` — incomplete raw
  history can never overwrite complete retained statistics. Full rebuild
  reports processed vs skipped keys.

### Department isolation
* Manager tokens are bound to one department; admin is unscoped.
* Daily/weekly list, raw detail and CSV export all pass through one
  scope-enforcing function. A manager passing another `department_id` gets
  `403`; missing/invalid tokens get `401`; admin-only routes reject managers.

## Migrations

Plain numbered SQL files in `internal/database/migrations/`, embedded with
`go:embed` and applied in lexical order, each in its own transaction. Add a
new `0003_*.sql` for any schema change — never edit an applied migration.

## Tests

```bash
make test        # vet + unit tests + race-enabled Postgres integration tests
```

Each integration test creates an isolated throwaway database, so runs don't
interfere and can target any dev Postgres via `TEST_DATABASE_URL`. Coverage:

* privacy filtering never reaches raw/statistics,
* cross-day / cross-timezone day and week bucketing,
* idempotency, hard conflict and whole-batch rollback,
* late snapshot recompute vs full rebuild equality,
* classification & policy version immutability, priority/rule-id tie-break,
* concurrent rebuild + ingestion (no lost/double-counted rows),
* purge boundary: summaries retained, partial coverage blocks rebuild,
* manager isolation on list, detail and CSV export.

## Layout

```
cmd/desklens            HTTP entrypoint
internal/database       connection + embedded SQL migrations
internal/repo           sqlx data access, recompute SQL
internal/ingest         batch validation, filters, version stamping, tx pipeline
internal/aggregate      rebuild service with coverage gates + advisory locks
internal/policy         monitoring-window / exclusion / exemption logic
internal/classify       glob classifier with stable priority ordering
internal/timeutil       UTC truncation, local date, ISO week helpers
internal/auth           bearer-token middleware and role guards
internal/api            echo handlers
examples                sample payload and curl walkthrough
```
