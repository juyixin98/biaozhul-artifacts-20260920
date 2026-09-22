# renderq — local VFX render queue (Go + Chi + sqlc + PostgreSQL)

A backend-only render queue for **local PNG layer compositing**. It deliberately
does **no** video transcoding, GPU work or complex effects: a composition is a
JSON manifest of PNG layers, and each output frame is produced by alpha
("over") compositing those layers in order onto a transparent RGBA canvas.

## Guarantees implemented

- **Manifest validation.** A composition JSON is rejected when a layer
  references a missing asset, the dependency graph has a cycle, a layer falls
  outside the canvas, or an asset path escapes the project root
  (`../`, absolute, Windows roots, non-PNG).
- **Immutable versions.** Saving a composition copies the manifest JSON and a
  per-layer `(asset_id, sha256)` snapshot into `versions` /
  `version_resources`. Jobs bind to a concrete version and digest; re-uploading
  or editing assets afterward never changes a queued or running job.
- **Real rendering.** The worker decodes each referenced PNG from the
  content-addressed blob store, verifies its sha256, composites it with
  straight-alpha Porter-Duff "over" math in manifest order, and encodes a real
  PNG. No fixed/placeholder images and no empty files are ever produced.
- **Priority + FIFO, capped concurrency.** Priority `1..10` (1 highest), then
  `enqueued_at`, then frame number. At most **2** worker loops (enforced in
  config); per-frame `FOR UPDATE SKIP LOCKED` claims mean two workers never
  process the same frame.
- **Persistent leases + fencing generations.** Each claim stores
  `leased_by`, `leased_until`, bumps `generation`, and the worker heartbeats
  the lease. Expired leases may be reclaimed; a late submit from the old
  generation updates zero rows and cannot overwrite the new result.
- **Retries with locatable failures.** One initial try + up to 3 retries
  (`max_attempts = 4`). Each frame row carries `attempts`, `last_error`,
  output digest/size. A job is only `succeeded` when **every** frame is
  `succeeded`; any exhausted frame makes the job `failed` while the
  successful frames remain downloadable.
- **Crash recovery.** On startup, before any claim: leases of the dead process
  are invalidated (generation bump) and their frames requeued; running jobs
  become queued again and rendering **continues from completed frames**;
  frames the DB called succeeded but whose file is missing/has a bad digest
  are reset and re-rendered (and a wrongly-`succeeded` job is reopened);
  leftover staging files and unacknowledged output files are swept. Frame
  bytes are written to a temp sibling, fsynced and atomically renamed, so a
  crash between DB commit and file install is repaired, not faked.
- **Single terminal state.** Worker completion and cancellation both take a
  `SELECT ... FOR UPDATE` lock on the job row; the frame submit additionally
  requires the job to still be `queued/running`. Whichever wins, the other
  affects zero rows. After cancellation no result is published and partial
  outputs are removed.
- **RBAC.** Requests authenticate with an API key (only the key's sha256 is
  stored). Admins can access every project and cancel any job; members only
  access projects they belong to (and only the owner/admin may cancel).

## Layout

```
cmd/server/            HTTP API + workers entrypoint
cmd/genpng/            writes sample PNG layers into samples/
migrations/            embedded SQL (schema) + migrator
db/queries/*.sql       sqlc queries
internal/api/          Chi router, auth, RBAC, handlers, version freeze
internal/compose/      real PNG alpha compositing
internal/worker/       claim/heartbeat/render/submit, retries, Recover
internal/storage/      content-addressed blobs + atomic output publication
internal/domain/       manifest model, path validation, DAG/bounds checks
internal/db/dbgen/     generated sqlc code (do not edit by hand)
tests/                 integration tests (PostgreSQL required)
```

## Run with Docker

```bash
docker compose up --build db api
# generate sample PNGs into ./samples
docker compose run --rm gen-samples
```

The API listens on `http://localhost:8080`. Health check: `GET /healthz`.

Bootstrap dev API keys (override via env in `docker-compose.yml`):

| User  | Role   | Key             |
|-------|--------|-----------------|
| admin | admin  | `dev-admin-key` |
| alice | member | `dev-alice-key` |
| bob   | member | `dev-bob-key`   |

Configuration env: `HTTP_ADDR`, `DATABASE_URL`, `DATA_DIR`, `WORKERS`
(1 or 2), `LEASE_SECONDS`, `RENEW_SECONDS`, `POLL_MILLIS`,
`ADMIN_API_KEY`, `ALICE_API_KEY`, `BOB_API_KEY`.

## Run locally

```bash
go run ./cmd/genpng samples
DATABASE_URL=postgres://renderq:renderq@localhost:5432/renderq?sslmode=disable \
  go run ./cmd/server
```

Regenerate sqlc after editing `db/queries/*.sql` or `migrations/*.sql`:

```bash
sqlc generate
```

## End-to-end walkthrough

```bash
KEY=dev-alice-key
BASE=http://localhost:8080

# 1. project
PID=$(curl -s -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"demo"}' $BASE/projects | jq -r .id)

# 2. upload two PNG layers (multipart: path + file)
curl -s -H "X-API-Key: $KEY" -F path=bg.png -F file=@samples/bg.png \
  $BASE/projects/$PID/assets
curl -s -H "X-API-Key: $KEY" -F path=fg.png -F file=@samples/mid.png \
  $BASE/projects/$PID/assets

# 3. composition (canvas matches layer bounds)
CID=$(curl -s -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"comp","width":120,"height":80}' \
  $BASE/projects/$PID/compositions | jq -r .id)

# 4. freeze an immutable version (validates deps/cycles/bounds/paths)
VID=$(curl -s -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"manifest":{"width":120,"height":80,"layers":[
        {"id":"bg","asset":"bg.png","x":0,"y":0},
        {"id":"fg","asset":"fg.png","x":20,"y":15,"opacity":0.9}]}}' \
  $BASE/compositions/$CID/versions | jq -r .id)

# 5. enqueue frames 0..4 at priority 5
JID=$(curl -s -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"priority":5,"frameStart":0,"frameEnd":4}' \
  $BASE/versions/$VID/jobs | jq -r .id)

# 6. poll; inspect per-frame status and errors
curl -s -H "X-API-Key: $KEY" $BASE/jobs/$JID | jq
curl -s -H "X-API-Key: $KEY" $BASE/jobs/$JID/frames | jq

# 7. fetch an output frame and the summary
curl -s -H "X-API-Key: $KEY" -o frame0.png $BASE/jobs/$JID/frames/0/png
curl -s -H "X-API-Key: $KEY" $BASE/jobs/$JID/summary | jq

# admin cancels any job (owner may cancel their own)
curl -s -X POST -H "X-API-Key: dev-admin-key" $BASE/jobs/$JID/cancel
```

See `samples/manifest.example.json` for a manifest with dependencies,
opacity and per-frame offsets.

## Tests

Pure unit tests (no database):

```bash
go test ./internal/domain/... ./internal/compose/...
```

Integration tests spin up a fresh, uniquely named PostgreSQL database per
`testutil.Harness`, migrate it and tear it down afterwards. Provide a
superuser URL:

```bash
export RENDERQ_TEST_DATABASE_URL='postgres://renderq:renderq@localhost:5432/postgres?sslmode=disable'
go test ./... -count=1
```

Coverage includes:

- version freeze immutability and version-number monotonicity;
- missing-resource, dependency-cycle, path-traversal and out-of-bounds
  rejection at freeze time;
- priority/FIFO dispatch ordering;
- two workers claiming each frame exactly once (`SKIP LOCKED`);
- lease expiry → reclaim with generation bump;
- late generation submit updating zero rows / not overwriting;
- retry-exhaustion (4 attempts), transient-retry success, partial failure not
  reported as whole success;
- cancel of a queued job, cancel-vs-complete race across repeated runs with
  exactly one terminal state and no output after a cancel win;
- crash recovery: continue from completed frames, reopen a succeeded job when
  an output file is missing, sweep staged files.

## Notes on safety boundaries

- Asset paths are validated against traversal before any filesystem access;
  blobs are content-addressed and served from the data directory only.
- Uploads are hard-limited to 32 MiB and must be fully valid PNGs.
- The queue trusts only the local filesystem and the local database; there is
  no shell-out, no transcoder and no network fetch of assets.
