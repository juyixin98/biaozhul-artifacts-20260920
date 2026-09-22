# GeoTerritory API

Base URL: `http://localhost:8080`
All data endpoints are under `/api/v1` and require the header:

```
X-API-Key: demo-key-acme
```

Two demo keys are seeded: `demo-key-acme` (Acme Logistics) and `demo-key-beta` (Beta Fleet). The key identifies the **organization**, which is the sole tenancy/authorization boundary. Every read and write is scoped to the caller's organization; cross-organization rows are invisible (404 on direct lookup, absent from searches).

All request/response bodies are JSON. Times are UTC RFC3339.

---

## 1. Core semantics

### Polygons

* A region is a **closed simple polygon** supplied as an **open ring** JSON array of vertices `[{"lng":..,"lat":..}, …]`. The closing edge back to the first vertex is implicit.
* At least **3 distinct vertices** are required.
* Coordinates: latitude `[-90, 90]`, longitude `[-180, 180]`. Non-finite numbers rejected.
* Rejected, with HTTP 400 and a specific message, when the polygon:
  * has a consecutive duplicate vertex (including a trailing repeat of the first),
  * has zero signed area (collinear / degenerate),
  * has any pair of non-adjacent edges that intersect or touch (no self-intersection, no self-touch "pinch"),
  * **crosses the antimeridian**: any edge whose longitude delta exceeds 180°. Such shapes must be split into two polygons along the 180° meridian and submitted as two regions. The system never silently interprets a 358° edge as the 2° edge.

### Boundary point rule

A point falling **exactly on an edge or on a vertex is inside** (inclusive boundary, tolerance 1e-9°).

### Overlap resolution

When a point is inside multiple regions:

1. smallest **priority** wins — `1` is highest, `5` lowest;
2. ties (same priority) break by the smallest **region ID**.

A point inside no region is **unassigned** (`region_id: null`).

### Immutable versions and assignment basis

Each publish of a region creates an immutable `region_versions` row (`status = building`) and a persistent `reassign_jobs` row. Until the job applies:

* the new geometry is not used by any point write or query;
* existing points keep their old `region_id`, `region_version_id` and `assign_set_seq`.

When the job completes it performs, in one transaction, the **atomic switch**: all point assignments are updated, the new version becomes `active`, the old version of that region becomes `superseded`, and the organization's `active_set_seq` advances. Old versions are retained forever and every point records `region_version_id` + `assign_set_seq` identifying exactly which set produced its assignment.

### Point writes and optimistic concurrency

* `(org_id, external_id)` is unique; batch import is idempotent by external ID.
* Replaying a row with unchanged coordinates returns `unchanged` and does not bump `version`.
* Changing an existing point's coordinates requires the correct `expected_version`; a missing or stale token yields a row-level `VERSION_CONFLICT` (HTTP 200 overall — see below) and changes nothing.
* Concurrent writers therefore cannot lose updates: one wins, the other gets a conflict and retries with the new version.
* Batch import accepts **at most 1000 rows**; per-row validation errors are reported individually and all other rows still commit.

---

## 2. Endpoints

### Health

```
GET /health
→ 200 {"status":"ok"}
```

### Regions

#### `POST /api/v1/regions` — create region + first version

Request:
```json
{
  "name": "beijing-core",
  "priority": 1,
  "polygon": [
    {"lng": 116.20, "lat": 39.80},
    {"lng": 116.60, "lat": 39.80},
    {"lng": 116.60, "lat": 40.05},
    {"lng": 116.20, "lat": 40.05}
  ]
}
```

`202 Accepted`:
```json
{
  "region_version": {"id": 16, "region_id": 12, "version": 1, "priority": 1, "status": "building", "set_seq": 1, ...},
  "reassign_job":   {"id": 16, "target_seq": 1, "status": "pending", ...},
  "message": "..."
}
```
Errors: `400` invalid polygon / priority outside 1..5 / duplicate name.

#### `POST /api/v1/regions/:id/versions` — publish a new immutable version

Same body shape (the `name` field is ignored). Returns `202` with the new `region_version` (version incremented) and a recompute job. An identical geometry+priority publish is rejected with `400`.

#### `GET /api/v1/regions` — list regions

```json
{"regions": [{"id": 12, "name": "beijing-core", "active_version_id": 17, ...}]}
```

#### `GET /api/v1/regions/:id` — region detail with full version history

```json
{
  "region": {...},
  "versions": [
    {"id": 17, "version": 2, "priority": 1, "status": "active", "set_seq": 3, ...},
    {"id": 16, "version": 1, "priority": 1, "status": "superseded", "set_seq": 1, ...}
  ]
}
```

#### `GET /api/v1/jobs/:id` — recompute job status

```json
{"id": 18, "target_seq": 3, "status": "done", "started_at": "...", "finished_at": "...", "error": ""}
```
Statuses: `pending → running → done`; a stale/duplicate job ends `superseded`; an unexpected failure ends `failed` (with `error`) and is retried by the worker on next poll — pending/running jobs are also adopted after a crash.

### Points

#### `POST /api/v1/points/batch` — idempotent batch import (max 1000)

```json
{
  "points": [
    {"external_id": "P1", "lat": 39.95, "lng": 116.5},
    {"external_id": "P2", "lat": 39.60, "lng": 116.20, "expected_version": 3}
  ]
}
```

`200` (always, unless the whole batch is structurally invalid / oversize):
```json
{
  "total": 2, "succeeded": 1, "failed": 1,
  "row_results": [
    {"external_id": "P1", "status": "created", "version": 1, "point": {"id": 42, "region_id": 12, "region_version_id": 16, "assign_set_seq": 1, ...}},
    {"external_id": "P2", "status": "error", "error_code": "VERSION_CONFLICT", "error": "version conflict: expected 3 but point is at version 2 ..."}
  ]
}
```

Row `status`: `created` | `updated` | `unchanged` | `error`.
Row error codes: `EXTERNAL_ID_REQUIRED`, `DUPLICATE_IN_BATCH`, `INVALID_COORDINATES`, `VERSION_CONFLICT`, `INTERNAL`.

Successful rows include the resulting point with its current assignment and basis.

#### `PATCH /api/v1/points/:external_id` — single optimistic coordinate update

```json
{"lat": 39.61, "lng": 116.21, "expected_version": 1}
```
`200 {"status": "updated", "point": {...version 2...}}`
`409` on stale/missing `expected_version`; `404` if the point does not exist in your organization.

#### `GET /api/v1/points/:external_id`

Returns the point with `region_id`, `region_version_id` (the exact basis version) and `assign_set_seq`. `404` for unknown IDs **and for other organizations' IDs**.

### Spatial queries (organization scoped)

#### `GET /api/v1/points/bbox/search` — bounding box

```
GET /api/v1/points/bbox/search?min_lat=39&max_lat=41&min_lng=116&max_lng=117&limit=1000
```
Borders are inclusive. `min_lng > max_lng` is rejected with `400` (no antimeridian wrapping; split the query). `limit` defaults/caps at 1000.

```json
{"count": 3, "points": [{"external_id": "P1", ...}]}
```

#### `GET /api/v1/points/nearest` — nearest N by Haversine

```
GET /api/v1/points/nearest?lat=31.23&lng=121.47&n=10
```
* `n` optional, default 10, **maximum 50** (larger → `400`).
* Distance is the great-circle distance in metres (mean Earth radius 6 371 008.8 m).
* Ordering: ascending distance, then ascending point **ID** on exact ties — fully deterministic.

```json
{"count": 2, "points": [{"external_id": "S002", "distance_meters": 0, ...}]}
```

---

## 3. Error format

Request-level errors:
```json
{"error": "human readable message"}
```
with status `400` (validation), `401` (missing/unknown API key), `404` (not found in your org), `409` (optimistic version conflict), `500`.

Batch row errors appear inline per row (HTTP stays 200) so a single bad row never fails the successful rows.

---

## 4. Worked walkthrough

```bash
B=http://localhost:8080
A='-H X-API-Key:demo-key-acme -H Content-Type:application/json'

# 1. Two overlapping regions: core wins the overlap by priority.
curl -s $A -X POST $B/api/v1/regions -d '{
  "name":"beijing-core","priority":1,
  "polygon":[{"lng":116.2,"lat":39.8},{"lng":116.6,"lat":39.8},{"lng":116.6,"lat":40.05},{"lng":116.2,"lat":40.05}]}'
curl -s $A -X POST $B/api/v1/regions -d '{
  "name":"beijing-metro","priority":3,
  "polygon":[{"lng":116.0,"lat":39.65},{"lng":116.9,"lat":39.65},{"lng":116.9,"lat":40.3},{"lng":116.0,"lat":40.3}]}'

# 2. Wait for the recompute jobs (poll GET /api/v1/jobs/:id), then import.
curl -s $A -X POST $B/api/v1/points/batch -d '{"points":[
  {"external_id":"P-OVERLAP","lat":39.95,"lng":116.5},
  {"external_id":"P-CORE-ONLY","lat":39.82,"lng":116.3},
  {"external_id":"P-OUTSIDE","lat":20,"lng":20}
]}'

# P-OVERLAP -> beijing-core (priority 1); P-CORE-ONLY -> core; P-OUTSIDE -> null.

# 3. Publish a new core version; watch the job and the basis change atomically.
curl -s $A -X POST $B/api/v1/regions/12/versions -d '{
  "priority":1,
  "polygon":[{"lng":116.3,"lat":39.85},{"lng":116.6,"lat":39.85},{"lng":116.6,"lat":40.05},{"lng":116.3,"lat":40.05}]}'
```
