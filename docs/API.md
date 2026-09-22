# GeoTerritory API

All endpoints (except `/healthz`) require an organization API key, sent as:

```
X-API-Key: <key>
```
or `Authorization: Bearer <key>`.

Every request is scoped to the organization resolved from the key. A key can
only ever read or write its own org's regions and points; there is no way to
address another org's data.

All coordinates are WGS-84 latitude/longitude in decimal degrees. Times are
UTC. The base path is `/v1`.

## 1. Geometry rules

### Polygons

A region is one **closed, simple polygon**. Vertices are supplied as a ring
**without repeating the first vertex at the end** (the closing edge is
implicit):

- at least **3 distinct vertices**;
- latitude in `[-90, 90]`, longitude in `[-180, 180]`, finite numbers;
- **no self-intersection**, including non-adjacent edges touching and a vertex
  lying on a non-incident edge (degenerate spikes);
- **non-zero area** (collinear rings rejected).

### Boundary points

A point lying **exactly on a polygon edge — including a vertex or a point on
the closing edge — is treated as inside** that region. Assignment responses
set `on_boundary: true` for these points so callers can distinguish them.

### Antimeridian (+/-180)

Polygons **must not cross the antimeridian**. Any edge whose longitude delta
is `>= 180°` is rejected with HTTP 400 (e.g. a vertex at `179` followed by one
at `-179`). Bounding-box queries with `min_lng > max_lng` are likewise
rejected. The system never silently interprets such input as the small
complementary polygon. To represent a territory near the dateline, split it
into multiple regions that stay within one side.

### Overlaps

When several regions contain a point, the winner is the region with the
smallest `priority` (1 is highest, 5 lowest); ties are broken by the smaller
region **id**, so the result is deterministic and stable. Points outside
every region are recorded as **unassigned** (`region_id = 0`).

## 2. Catalogs, versions and reassignment

Regions are published as immutable **catalog versions** per organization.

- `current_version` — the catalog queries and assignments are based on.
- `published_version` — advanced immediately when a region is published.
- Publishing creates the next immutable catalog and enqueues a durable
  **reassignment job**. While it runs, reads against `current_version` stay
  consistent with the old catalog. When the recompute is complete,
  `current_version` flips to the new version **atomically** (a compare-and-set
  inside the organization lock).
- Point assignments are stored per catalog version, so reading an old version
  keeps returning the territory the point belonged to at that time.

During a reassignment, newly imported or moved points are written against
**both** live versions, and the worker performs a final delta sweep under the
organization lock — no point can be missed. Worker writes use `INSERT IGNORE`
and the catalog flip is a CAS, so a late/stale job can never overwrite a newer
result.

## 3. Endpoints

### `GET /healthz`

Liveness + MySQL ping. No auth.

### `GET /v1/catalog`

Returns the catalog pointers and the regions in the current catalog:

```json
{
  "current_version": 2,
  "published_version": 2,
  "reassigning": false,
  "regions": [ { "region_id": 1, "name": "zone-a", "priority": 1,
                 "vertices": [ {"lat": ..., "lng": ...} ] } ]
}
```

### `POST /v1/regions`

Publish a region (creates it or publishes a new version of an existing name).
Body:

```json
{
  "name": "zone-a",
  "priority": 1,
  "vertices": [ {"lat": 40.0, "lng": -74.0}, {"lat": 40.1, "lng": -74.0},
                {"lat": 40.1, "lng": -73.9} ]
}
```

- `priority` 1..5 (required).
- `202 Accepted` with `catalog_version` and `job_id` on success.
- `400` for any geometry violation (message names the failing edge/vertex).
- `409` if a reassignment is already active for the org.

### `GET /v1/regions` / `GET /v1/regions/:id`

List regions (optionally `?active=true`) or fetch one with its latest
snapshot.

### `GET /v1/regions/:id/versions/:version`

Fetch one immutable region polygon snapshot.

### `POST /v1/regions/:id/deactivate`

Mark a region inactive; it is excluded from the next published catalog.

### `POST /v1/points/batch`

Batch import/upsert of up to **1000** points. Body:

```json
{ "points": [
  {"external_id": "p1", "lat": 40.0, "lng": -74.0, "expected_version": 0},
  {"external_id": "p2", "lat": 41.0, "lng": -73.0}
] }
```

Idempotency / concurrency rules (per row, keyed by `external_id` within the
org):

| Situation | `expected_version` | Result |
|---|---|---|
| id does not exist | `0` or omitted | insert, returns `version = 1` |
| id does not exist | `N > 0` | — | `VERSION_CONFLICT` |
| exists, **same coordinates** | any | idempotent, version unchanged |
| exists, **coordinates changed** | omitted | `VERSION_CONFLICT` (must supply it) |
| exists, **coordinates changed** | current seq | update, `version` increments |
| exists, **coordinates changed** | stale seq | `VERSION_CONFLICT`, returns current seq |

Response (HTTP 200 even when some rows fail — successful rows are kept):

```json
{ "total": 2, "success": 1, "failed": 1,
  "results": [
    {"index": 0, "external_id": "p1", "status": "OK", "id": 10, "version": 1, "created": true},
    {"index": 1, "external_id": "p2", "status": "VERSION_CONFLICT", "version": 7,
     "error": "point was modified concurrently; refetch its version and retry"}
  ] }
```

The whole batch runs in one transaction under the organization lock, so
concurrent batches for the same external id are serialized and an update can
never be lost. Coordinate validation failures come back as
`VALIDATION_ERROR` and do not affect other rows.

### `GET /v1/points/:external_id?catalog_version=current`

Resolve one point and its assignment under a catalog version. `catalog_version`
accepts `current` (default), `published`, or an explicit integer.

### `GET /v1/points/bbox`

Bounding-box query, restricted to the caller's organization.

Query params: `min_lat,max_lat,min_lng,max_lng` (all required),
`assigned_only=true` (optional), `limit` (1..1000, default 1000), `offset`.
`min_lng <= max_lng` is enforced (no dateline wrap). Points are joined with
their assignment under the requested catalog version.

### `GET /v1/points/nearest`

Nearest-N points by great-circle distance.

Query params: `lat,lng` (anchor, required), `n` (1..**50**, default 10),
`assigned_only=true`, `catalog_version`. Distance is **Haversine** (meters,
Earth radius 6,371,000 m); equal distances are broken by ascending point id so
ordering is stable. Example row:

```json
{ "id": 10, "external_id": "p1", "lat": 40.0, "lng": -74.0,
  "catalog_version": 2, "region_id": 1, "region_version": 1,
  "region_name": "zone-a", "on_boundary": false, "distance_m": 132.4 }
```

### `GET /v1/jobs/:id`

Reassignment job status: `PENDING`, `RUNNING`, `DONE`, `FAILED` with
`total_points`, `processed_points`, heartbeat and completion time.

## 4. Errors

| Status | Meaning |
|---|---|
| 400 | malformed request / invalid geometry or parameters |
| 401 | missing/invalid API key |
| 404 | resource or catalog version not found (within the org) |
| 409 | version conflict, active reassignment, or org lock busy |
| 500 | unexpected server error |

Error bodies are `{"error": "...", ...}`.
