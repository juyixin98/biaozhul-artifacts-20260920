# SynapticGo

Local model-experiment backend for a single, well-defined model: a **one-layer
linear classifier with softmax**. It covers dataset upload (chunked,
resumable, content-verified), immutable model version registration, and real
CPU inference, all backed by PostgreSQL and a content-addressed file store.

There are deliberately **no** complex networks, training, or media handling in
this iteration.

## Features

### Datasets
- **Chunked upload** (`PUT /datasets/:id/chunks/:idx`) records total size, the
  exact byte offset/length of each slot, and its SHA-256.
- **Idempotent retries**: re-sending an identical chunk returns the existing
  record (`200`, `idempotent: true`). Sending different content for the same
  slot, or a `Digest` header that does not match the actual bytes, is rejected
  (`422`).
- **Out-of-order and resumable**: upload chunks in any order. Query
  `GET /datasets/:id/chunks` to see which slots are filled. Publishing with
  missing chunks fails; finish the upload and publish again.
- **Whole-file verification**: at publish the chunks are concatenated and
  hashed; if the result does not equal the declared `whole_digest`, the dataset
  is never marked ready.
- **Crash-consistent merge**: assembly stages to a temp file, then metadata,
  refcounts, and the `ready` flip commit in one DB transaction. A half-finished
  file is never usable. On startup the server reconciles the filesystem with
  the database (orphan blobs removed, rolled-back quarantines restored).
- **Content sharing with owner isolation**: identical bytes share one blob and
  one `objects` row via reference counting, but every read/write is checked
  against the dataset owner.

### Models & inference
- A model version binds **dataset digest, input dimension, class table, and
  weights** and is **immutable after release** (there is no update API;
  versions are append-only per model name).
- Weights are a binary float32 blob (`[W | b]`, little-endian) validated for
  shape and finiteness at registration and again at load.
- Inference is a genuine Go CPU forward pass
  `z = Wx + b; p = softmax(z)` with numerical stabilization (max subtraction),
  shape checking, and NaN/Inf rejection — no fixed or stubbed predictions.
- Every prediction writes an **experiment record** (input digest, predicted
  class/index, confidence, latency).

### Lifecycle & comparison
- Deleting a dataset referenced by a model version is refused (`409`).
- Deleting a model version with experiment records is refused (`409`).
- A shared blob is deleted from disk only when its last reference goes. The
  refcount decrement-to-zero and the file move hold a per-digest advisory lock,
  so a concurrent new reference cannot remove a blob still in use.
- `GET /experiments/compare?a=&b=` compares two versions **only when their class
  tables have the same digest**. Reordered or different label sets return
  `422`: their per-class metrics are not comparable, even if the class counts
  match.

## Quick start (Docker)

```bash
docker compose up --build
```

The server runs on http://localhost:8080 and applies migrations on startup.
On a host where those ports are taken, override them:

```bash
HTTP_HOST_PORT=18091 DB_HOST_PORT=55477 docker compose up --build
```

Create two demo users (keys are shown exactly once):

```bash
docker compose --profile seed up seed
```

## Local development

Requires Go 1.25+ and PostgreSQL 16.

```bash
createdb synapticgo            # or: see scripts/init-db.sh
export DATABASE_URL='postgres://synaptic:synaptic@localhost:5432/synapticgo?sslmode=disable'
export DATA_DIR=./data
go run ./cmd/server
```

Migrations are embedded (`internal/db/migrations_sql`) and applied
automatically at boot.

### Walkthrough

```bash
bash scripts/walkthrough.sh     # creates a user, uploads, publishes, registers
                                # a model, runs and compares predictions
```

## API

All routes except `POST /api/v1/users` require
`Authorization: Bearer <api_key>`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/users` | Create a user, returns the API key once |
| POST | `/api/v1/datasets` | Create dataset shell (`total_size`, `chunk_size`, optional `whole_digest`) |
| GET | `/api/v1/datasets` | List own datasets |
| GET | `/api/v1/datasets/:id` | Get one dataset |
| DELETE | `/api/v1/datasets/:id` | Delete (refused if a model references it) |
| PUT | `/api/v1/datasets/:id/chunks/:idx` | Upload one chunk (`Digest: sha-256=<hex>`) |
| GET | `/api/v1/datasets/:id/chunks` | Filled-slot map (resume support) |
| POST | `/api/v1/datasets/:id/publish` | Assemble, verify, mark ready |
| GET | `/api/v1/datasets/:id/content` | Download assembled bytes |
| POST | `/api/v1/models` | Register an immutable version |
| GET | `/api/v1/models` / `/models/:id` | List / get own versions |
| DELETE | `/api/v1/models/:id` | Delete (refused with experiment records) |
| POST | `/api/v1/models/:id/predict` | Run the linear+softmax forward pass |
| GET | `/api/v1/models/:id/experiments` | List inference records |
| GET | `/api/v1/experiments/compare?a=&b=` | Compare two same-class-table versions |
| GET | `/healthz` | Liveness |

### Weight encoding

```
uint32 input_dim, uint32 num_classes,
float32 W[num_classes * input_dim]   (row-major, little-endian),
float32 b[num_classes]               (little-endian)
```

Send the bytes as standard base64 in `weights_base64`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `HTTP_ADDR` | `:8080` | Listen address |
| `DATABASE_URL` | `postgres://synaptic:synaptic@localhost:5432/synapticgo?sslmode=disable` | DSN |
| `DATA_DIR` | `./data` | Object-store root |

## Testing

```bash
go test ./...
go test -race ./...
```

Integration tests provision an isolated, migrated database per test
(`CREATE DATABASE syn_test_…`) and a temp data directory. Point them at a
maintenance DSN with `TEST_DATABASE_URL` if needed; the connecting role needs
`CREATEDB`. The suite covers:

- out-of-order uploads and idempotent retransmission,
- content/digest conflicts and whole-digest mismatch,
- publish interrupted before commit and recovery on restart,
- orphan blob from a rename/crash window,
- concurrent publishes (exactly one wins),
- shared-content reference counting and cross-user isolation,
- acquire/release races never deleting an in-use blob,
- inference numerics against hand-computed softmax plus shape/NaN rejection,
- model immutability/tamper evidence, delete reference checks, and
  class-table-gated comparison.

## Layout

```
cmd/server            entrypoint: migrate, recover, serve
internal/db           embedded SQL migrations
internal/store        content-addressed FileStore + refcount ObjectService + recovery
internal/dataset      chunk upload, idempotency, verified crash-safe publish
internal/modelx       immutable model version registration/deletion
internal/inference    linear + softmax CPU forward pass
internal/experiments  run records + class-table-gated comparison
internal/httpx        Echo router, auth middleware, handlers
internal/testutil     per-test database harness
scripts               init-db.sh, walkthrough.sh
examples              make_weights.py weight-blob generator
```
