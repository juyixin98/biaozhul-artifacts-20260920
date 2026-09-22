# GeoTerritory

Geographic territory assignment backend. Organizations define territories as
closed polygons; tracked points are assigned to exactly one territory (or
left unassigned) under an immutable, versioned territory set. Built with
**Go + Gin + GORM + MySQL 8**, started with **Docker Compose**.

Out of scope by design: CRM workflows, map UIs, and external geocoding /
geometry services — point-in-polygon and distance math are implemented
locally in `internal/geometry`.

---

## Quick start

```bash
docker compose up --build
```

This starts MySQL 8 and the server (applies migrations and seeds demo data
automatically). The server listens on the host port `8080` (edit
`docker-compose.yml` if that port is taken):

```bash
curl -s http://localhost:8080/health
# {"status":"ok"}

curl -s -H 'X-API-Key: demo-key-acme' http://localhost:8080/api/v1/regions
```

Demo organizations / API keys (seed data):

| Organization   | API key         |
|----------------|-----------------|
| Acme Logistics | `demo-key-acme` |
| Beta Fleet     | `demo-key-beta` |

Acme comes with five sample points (`S001`…`S005`); create regions through
the API to see them assigned.

### Configuration (environment variables)

| Variable          | Default        | Meaning                                   |
|-------------------|----------------|-------------------------------------------|
| `HTTP_ADDR`       | `:8080`        | Listen address                            |
| `MYSQL_HOST`      | `127.0.0.1`    | Host                                      |
| `MYSQL_PORT`      | `3306`         | Port                                      |
| `MYSQL_USER`      | `geoterritory` | User                                      |
| `MYSQL_PASSWORD`  | `geoterritory` | Password                                  |
| `MYSQL_DATABASE`  | `geoterritory` | Database                                  |
| `MYSQL_DSN`      | _(assembled)_  | Full DSN override                         |
| `SEED_SAMPLES`   | `true`         | Seed demo orgs/points on boot (idempotent) |

Migrations are embedded SQL files (`internal/store/migrations/*.sql`) applied
at startup inside transactions and tracked in `schema_migrations`.

---

## Rules and guarantees

### Geometry

* Closed polygon supplied as an open ring, **≥ 3 distinct vertices**.
* Latitude `[-90, 90]`, longitude `[-180, 180]`; NaN/Inf rejected.
* Rejected: zero-length edges, zero-area (degenerate) rings,
  self-intersecting and self-touching rings.
* **Antimeridian: explicitly rejected.** An edge spanning > 180° of longitude
  yields a `400` naming the edge. Model such a territory as two polygons
  split on the 180° meridian. The same applies to antimeridian-crossing
  bounding-box queries. Nothing is ever silently wrapped or reinterpreted.
* **Boundary rule: inclusive** — a point exactly on an edge or vertex (within
  1e-9°) is inside. See `docs/API.md`.

### Assignment

* Pure local implementation (ray casting + boundary check,
  `internal/geometry/polygon.go`).
* Overlap: lowest **priority number wins (1 highest … 5 lowest)**; equal
  priorities break by lowest **region ID**.
* Outside every region: **unassigned** (`region_id = null`).
* Each point persists its basis: `region_version_id` + `assign_set_seq`.

### Versioning and recompute

* Every region create / change produces an **immutable version**
  (`building` → `active` → `superseded`; rows are never mutated after
  activation) and a durable `reassign_jobs` row with a monotonically
  increasing `target_seq`.
* **Atomic switch:** recomputed results are staged in
  `assignment_staging`; a single transaction then writes all point
  assignments, flips version statuses and advances `active_set_seq`. Until it
  commits, reads and writes keep using the previous complete set — old
  versions stay consistent.
* **No missed moves during recompute:** point-write transactions and the
  switch transaction both `SELECT … FOR UPDATE` the organization row; the
  switch additionally locks the org's points and, for any point whose version
  changed while staging ran, recomputes the assignment live against the
  target set.
* **No lost updates:** point coordinate changes use optimistic concurrency
  (`WHERE version = expected_version`); the loser gets HTTP 409 / a
  `VERSION_CONFLICT` row error and retries.
* **Stale-job guard:** the switch refuses any job with
  `target_seq <= active_set_seq` (marked `superseded`), and only applies the
  immediate successor seq, so a late commit from an old/crashed job can never
  overwrite newer results.
* **Crash recovery:** pending/`running` jobs are claimed with
  `FOR UPDATE SKIP LOCKED`; an adopted job clears and rebuilds its staging
  area, so an interrupted process leaves no partial effects. Multiple server
  replicas can run the worker safely.

### Ingest and queries

* Batch import ≤ **1000** rows, idempotent on `(org, external_id)`; coordinate
  changes require `expected_version`; per-row errors with all other rows
  retained.
* Bounding-box search (inclusive borders, org scoped).
* Nearest **N ≤ 50** points by **Haversine** (metres, R = 6 371 008.8 m),
  ties broken by point ID.
* Every query filters `org_id` in SQL; a foreign key/ID from another
  organization behaves as not-found.

Full HTTP reference: [`docs/API.md`](docs/API.md).
Sample geometries (valid + every invalid class):
[`examples/sample-polygons.json`](examples/sample-polygons.json).

---

## Project layout

```
cmd/server/            HTTP bootstrap, router, graceful shutdown
internal/
  geometry/            polygon validation, point-in-polygon, Haversine (pure Go)
  models/              GORM-mapped tables
  store/               embedded SQL migrations + demo seed
  service/             region publishing, point ingest/queries, effective sets
  jobs/                persistent recompute worker (stage + atomic apply)
  api/                 Gin middleware and handlers
  config/              env configuration
internal/store/migrations/  versioned schema SQL
tests/integration/     MySQL-backed end-to-end tests
examples/              sample polygons
docs/                  API.md
```

---

## Testing

Pure geometry unit tests (no infrastructure):

```bash
go test ./internal/geometry/
```

End-to-end tests need the MySQL from compose (already running on 3306):

```bash
docker compose up -d mysql
go test ./... -count=1
# optional: GEOTEST_DSN='user:pass@tcp(host:3306)/db?parseTime=true&multiStatements=true'
```

If MySQL is unreachable the integration package **skips** instead of failing.

Coverage (integration tests in `tests/integration/`):

| Concern               | Test |
|-----------------------|------|
| boundary points       | `TestPointInPolygon_BoundaryInclusive`, `TestAssignment_BoundaryPointInRegion` |
| self-intersection / degenerate / range / antimeridian | geometry unit tests + `TestPolygonValidation_AtService`, `TestHTTP_RegionLifecycle` |
| overlap priority + ID tie-break | `TestAssignment_OverlapPriority`, `TestAssignment_PriorityTieBrokenByRegionID` |
| recompute race (point moved during recompute not missed) | `TestRecompute_PointMovedDuringStagingIsNotMissed` |
| atomic switch / old-version consistency | `TestRecompute_AtomicSwitchAndOldVersionRead` |
| late stale job cannot overwrite | `TestRecompute_LateJobCannotOverwrite` |
| crash recovery        | `TestRecompute_CrashRecovery` |
| optimistic no-lost-update concurrency | `TestPointUpdate_ConcurrentNoLostUpdate` |
| idempotent batch + partial errors + 1000 cap | `TestBatch_*` |
| bbox / nearest cap / ID tie-break | `TestQueries_*` |
| authorization isolation | `TestHTTP_TenantIsolation`, `TestHTTP_AuthRequired`, beta-org checks in `TestQueries_*` |

---

## Notes on scale

Nearest-neighbour currently loads the organization's points and ranks them in
process (correct and fine for per-org point sets in the low millions; the
read is index-covered on `(org_id, lat, lng)`). At larger cardinalities the
natural evolution is a bounding-circle pre-filter or a MySQL spatial index —
deliberately not introduced prematurely.
