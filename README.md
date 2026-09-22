# VFX Render Queue (Go + Chi + sqlc + PostgreSQL)

A small, self-contained backend for **local PNG layer compositing** jobs. It
does not do video transcoding, GPU work, or fancy effects — just real,
deterministic, per-frame alpha ("source-over") compositing of PNG layers, with
a durable render queue.

## Features / guarantees

- **JSON-described compositions.** A version lists canvas size, frame count
  and ordered layers. Validation rejects missing resources, dependency cycles,
  unknown layers and out-of-canvas origins/path escapes.
- **Immutable, frozen versions.** Saving a composition creates a new
  `composition_versions` row containing the canonical spec hash **and a
  frozen snapshot of every layer's asset digest**. A render task binds to one
  version id + those digests; later edits create a new version and never alter
  a running task. The worker re-checks each blob's SHA-256 before rendering.
- **Real local rendering.** The worker decodes actual PNG files from disk and
  performs source-over alpha blending in topological dependency order. Output
  is a real PNG per frame plus its SHA-256/size — never a placeholder or an
  empty file.
- **Priority + FIFO scheduling.** Priority `1..10` (10 highest); equal
  priority is served in enqueue order (`enqueued_seq`); frames within a task
  run in frame order.
- **Hard cap of 2 concurrent workers** (`WORKER_CONCURRENCY`, clamped).
- **Per-frame claiming with persistent leases and generations.** Claims use
  `FOR UPDATE SKIP LOCKED`; each lease has a TTL and is renewed by heartbeat.
  Expired leases can be reclaimed, bumping the frame's `generation`; a late
  worker's commit is rejected and can never overwrite a newer generation's
  result.
- **Per-frame retries (max 3 attempts).** Failed frames keep their error
  message; permanently failed frames fail the task and cancel sibling frames.
- **Crash recovery.** A startup reconciler:
  - resets every stale open lease (its worker process is dead),
  - re-queues frames marked `succeeded` whose file is missing/empty/corrupt or
    whose checksum no longer matches,
  - deletes abandoned `.tmp-*` files and orphan outputs,
  - repairs task states so a partially rendered task is never reported as
    whole success.
  Already-finished frames resume from their checksummed files; they are not
  re-rendered.
- **Filesystem/DB crash window.** Output is written to a temp file, fsynced
  and atomically renamed *before* the database commit. A crash in between
  leaves an unreferenced file the reconciler deletes; the reverse order is
  impossible.
- **Single terminal state.** Cancellation and completion both take the task's
  row lock in one transaction, so cancel/complete race to exactly one
  terminal state. After cancellation, frames' generations are bumped and any
  in-flight worker commit is rejected — no results publish after cancel.
- **RBAC.** Bearer-token auth; `admin` can access/cancel anything; `member`
  can only touch projects they belong to (and outputs of tasks in those
  projects).

## Quick start (Docker)

```bash
docker compose up --build
```

This starts PostgreSQL 16 and the app (HTTP API + 2 workers), applies
migrations, and seeds demo data.

- API: http://localhost:8080
- PostgreSQL: localhost:5433 (user/pass/db `vfx/vfx/vfxqueue`)

Seeded dev tokens (local use only):

| Role  | Token                  |
|-------|------------------------|
| admin | `dev-admin-token-0001` |
| member (Alice, owns demo project) | `dev-alice-token-0002` |
| member (Bob, demo project member) | `dev-bob-token-0003` |

### Walkthrough

```bash
TOK=dev-alice-token-0002
BASE=http://localhost:8080

# Find the seeded project
curl -s -H "Authorization: Bearer $TOK" $BASE/v1/projects

# 1) Upload PNG layers (multipart field "file")
ASSET=$(curl -s -H "Authorization: Bearer $TOK" \
  -F file=@samples/bg.png \
  $BASE/v1/projects/$PROJECT/assets | tee /dev/stderr | jq -r .id)

# 2) Create a composition (freezes version 1)
curl -s -X POST -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -d @samples/composition.example.json \
  $BASE/v1/projects/$PROJECT/compositions   # substitute asset ids first

# 3) Enqueue frames 0..11 at priority 5
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"composition_id":"'$COMP'","frame_start":0,"frame_end":11,"priority":5}' \
  $BASE/v1/projects/$PROJECT/tasks

# 4) Watch status / fetch a frame
curl -s -H "Authorization: Bearer $TOK" $BASE/v1/tasks/$TASK
curl -s -H "Authorization: Bearer $TOK" \
  $BASE/v1/tasks/$TASK/frames/3/png -o frame_000003.png
```

Generate the sample PNG layers without running the service:

```bash
go run ./cmd/gensamples samples
```

## API summary

All routes except `/healthz` require `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/v1/me` | current user |
| POST | `/v1/projects` | create project (caller becomes owner) |
| GET  | `/v1/projects` | projects for the caller (all, for admin) |
| POST/GET | `/v1/projects/{id}/members` | manage members (owner/admin) |
| POST/GET | `/v1/projects/{id}/assets` | upload/list PNG assets (validated PNG only) |
| POST/GET | `/v1/projects/{id}/compositions` | freeze/list composition versions |
| POST/GET | `/v1/projects/{id}/tasks` | enqueue/list render tasks |
| GET  | `/v1/tasks/{id}` | task status + per-frame counts |
| POST | `/v1/tasks/{id}/cancel` | cancel (admin: any; member: own projects) |
| GET  | `/v1/tasks/{id}/frames` / `/frames/{idx}` | frame status, attempts, errors |
| GET  | `/v1/tasks/{id}/frames/{idx}/png` | rendered PNG |

Composition JSON:

```json
{
  "name": "demo",
  "canvas_width": 320,
  "canvas_height": 240,
  "frame_count": 12,
  "layers": [
    {"id": "background", "asset_id": "<uuid>", "x": 0, "y": 0},
    {"id": "overlay",    "asset_id": "<uuid>", "x": 60, "y": 50,
     "deps": ["background"]},
    {"id": "badge",      "asset_id": "<uuid>", "x": 220, "y": 16,
     "frame_start": 0, "frame_end": 11, "deps": ["overlay"]}
  ]
}
```

Draw order is a stable topological ordering over `deps` (dependencies draw
first). A zero `frame_start/frame_end` means the layer is visible on every
frame.

Enqueue JSON:

```json
{"composition_id": "<uuid>", "version_id": "<optional, defaults current>",
 "frame_start": 0, "frame_end": -1, "priority": 5}
```

`frame_end: -1` means the composition's last frame.

## Local development (without Docker)

Requires Go 1.22+, sqlc 1.25+, and PostgreSQL 16.

```bash
# start just the database
docker compose up -d postgres

export DATABASE_URL='postgres://vfx:vfx@localhost:5433/vfxqueue?sslmode=disable'
export DATA_DIR=./data

# regenerate query code after editing internal/db/queries/*.sql
sqlc generate

go run ./cmd/vfxqueue migrate
go run ./cmd/vfxqueue serve          # API + workers
go run ./cmd/vfxqueue api            # API only
go run ./cmd/vfxqueue worker         # workers only
```

### Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `DATABASE_URL` | `postgres://vfx:vfx@localhost:5433/vfxqueue?sslmode=disable` | Postgres DSN |
| `HTTP_ADDR` | `:8080` | listen address |
| `DATA_DIR` | `./data` | root for asset blobs and frame outputs |
| `WORKER_CONCURRENCY` | `2` | parallel renderers, **clamped to 2** |
| `LEASE_TTL_SECONDS` | `20` | frame lease lifetime |
| `HEARTBEAT_INTERVAL_SECONDS` | `7` | lease renewal interval |
| `POLL_INTERVAL_MILLIS` | `500` | idle worker poll interval |
| `SEED` | `1` (except `worker` mode) | seed demo data |

## Tests

Pure unit tests (spec validation/cycles, real PNG blending math, path
safety) need no database:

```bash
go test ./internal/compositor/ ./internal/storage/
```

Integration tests (`internal/integration`) use a real PostgreSQL. If
`TEST_DATABASE_URL` is set they use it; otherwise they automatically start an
ephemeral `postgres:16-alpine` container with Docker and destroy it at exit.

```bash
go test ./... -count=1
# or against your own database:
TEST_DATABASE_URL='postgres://...' go test ./internal/integration/ -count=1
```

Coverage includes the scenarios called out in the requirements:

- **version freezing** — frozen digest snapshot; tampered digest refuses to
  render rather than render changed content; multiple versions are isolated;
- **dependency validation** — missing resource, unknown dependency, direct
  and 3-node cycles, duplicate layers, out-of-bounds origins, bad frame
  windows;
- **lease competition** — 4 tasks × 5 frames under 2 workers, every frame
  claimed exactly once (`attempts == 1`);
- **late submit** — expired lease reclaimed (generation advances), old
  worker's commit rejected and cannot overwrite the new output;
- **crash recovery** — mid-task restart resumes from finished frames; a
  succeeded row whose file is lost is re-queued and re-rendered; temp/orphan
  files are cleaned; partial work is never reported as whole success;
- **cancel races** — queued cancel; 40 tasks hammered by concurrent cancel vs
  complete, asserting exactly one stable terminal state and no succeeded
  frame under a cancelled task;
- **retries** — 3 attempts then task failure with a locatable per-frame error
  and cancelled siblings; recovery on a later attempt;
- **authorization** — missing/bad token, member cross-project denial, admin
  cross-user cancel, non-PNG rejection;
- **HTTP end-to-end** — upload → freeze → enqueue → poll → fetch a real PNG.

## Layout

```
cmd/vfxqueue          entrypoint (serve|api|worker|migrate)
cmd/gensamples        writes samples/*.png
internal/db/migrations embedded SQL schema
internal/db/queries   sqlc queries (tasks.sql holds the concurrency logic)
internal/db/gen       generated sqlc code
internal/compositor   spec validation + real PNG source-over compositing
internal/worker       claim/heartbeat/render/submit, 2-worker cap
internal/reconcile    startup crash recovery
internal/apiserver    Chi HTTP API + RBAC
internal/storage      atomic writes, content-addressed blobs, path safety
internal/auth         bearer auth + project membership
internal/seed         demo data
internal/testsupport  ephemeral Postgres + fixtures for tests
```
