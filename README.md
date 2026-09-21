# Neural Training Workflow (CPU)

A local, reproducible neural-network training workflow built with
**FastAPI + SQLAlchemy + PostgreSQL + PyTorch**, runnable entirely via Docker.
Only CPU **Dense (Linear)**, **ReLU** and **Dropout** layers are supported.
There is deliberately no approval step, no GraphQL, and no visual network
editor.

## What it does

- **JSON network definitions** — layers and connections are validated for:
  - acyclic graph (topological / Kahn check),
  - a single input and a single output layer,
  - input/output **shape consistency** at every connection,
  - parameter ranges (feature widths, dropout `p`, etc.).
- **Immutable published architectures** — publishing a definition assigns an
  incrementing, content-fingerprinted **version**. A training job is bound to
  one architecture version, a dataset digest, a random seed and a hyperparameter
  set; nothing it depends on can change underneath it.
- **Safe dataset loading** — CSV or `.npy` from inside a whitelisted directory
  tree. `..` traversal, absolute escapes and **symlink escapes**
  (`data/x.csv -> /etc/passwd`) are resolved and rejected.
- **Fixed train/validation split** — sample indices are drawn once at job
  creation and stored; **resuming never re-splits**.
- **Durable training queue** — at most **3 running jobs per user**; jobs are
  claimed with `SELECT … FOR UPDATE SKIP LOCKED` and carry a **renewed lease**.
  An executor that loses its lease is rejected on every metric/checkpoint write.
- **Pause / resume / cancel at epoch boundaries** — checkpoints store model,
  optimizer, global/shuffle/numpy RNG state and the completed position. Files
  are written to a temp path, fsynced and atomically renamed **before** the DB
  reference is published. A corrupt latest checkpoint rolls back to the newest
  *valid* one.
- **SSE metric stream** — events carry gapless per-job sequence numbers; a
  disconnected client replays from `Last-Event-ID` / `?after=N`. Replay reads
  stored events, it never re-runs training.
- **Deterministic resume** — under a fixed seed, a paused/resumed run matches a
  continuously-trained run within the declared tolerance
  (`RESUME_RTOL=1e-4`, `RESUME_ATOL=1e-5`; in practice losses match to float
  exactness on CPU).

## Quick start (Docker)

```bash
docker compose up --build
```

This starts PostgreSQL and the API+worker on http://localhost:8000 (the database
is published on host port 5433 to avoid clashing with a local Postgres). Demo
data is generated automatically into the `/data` volume on first boot.

Run the end-to-end demo in another shell:

```bash
pip install httpx   # if needed
python scripts/run_demo.py
```

You can scale independent worker processes:

```bash
docker compose up --build --scale worker=2
```

## Local development (no Docker)

Requires Python 3.11+ and a PostgreSQL instance.

```bash
pip install -r requirements.txt

# one-time: create role/databases
sudo -u postgres psql -c "CREATE ROLE nn LOGIN PASSWORD 'nn';"
sudo -u postgres createdb -O nn nn_training
sudo -u postgres createdb -O nn nn_training_test

python scripts/generate_demo_data.py

DATABASE_URL=postgresql+psycopg2://nn:nn@localhost:5432/nn_training \
  uvicorn app.main:app --reload
```

Then open the interactive docs at http://localhost:8000/docs and run
`python scripts/run_demo.py`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | derived from `POSTGRES_*` | SQLAlchemy/psycopg2 DSN |
| `DATA_WHITELIST` | `./data` | `:`-separated dataset roots |
| `CHECKPOINT_DIR` | `./checkpoints` | checkpoint file root |
| `MAX_RUNNING_PER_USER` | `3` | concurrent running jobs per user |
| `LEASE_SECONDS` | `30` | claim lease length |
| `HEARTBEAT_INTERVAL` | `5` | worker lease-renewal interval |
| `MAX_CHECKPOINTS` | `3` | valid checkpoints retained per job |
| `ENABLE_WORKER` | `1` | run the in-process worker pool in the API |
| `WORKER_CONCURRENCY` | `2` | worker threads per process |
| `RESUME_RTOL` / `RESUME_ATOL` | `1e-4` / `1e-5` | declared consistency tolerance |

## API overview

All per-user calls require an `X-User-Id` header.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v1/architectures` | publish (version) a network definition |
| `GET  /api/v1/architectures/{id}` | fetch an immutable version |
| `POST /api/v1/datasets` | register a whitelisted CSV/npy dataset |
| `POST /api/v1/jobs` | enqueue a job (bound to arch, data digest, seed, hparams) |
| `GET  /api/v1/jobs/{id}` | job status and progress |
| `POST /api/v1/jobs/{id}/pause` | pause at the next epoch boundary |
| `POST /api/v1/jobs/{id}/resume` | re-queue a paused job |
| `POST /api/v1/jobs/{id}/cancel` | cancel (immediate if not running) |
| `GET  /api/v1/jobs/{id}/events?after=N` | gapless event replay |
| `GET  /api/v1/jobs/{id}/events/stream` | SSE feed (supports `Last-Event-ID`) |

### Network JSON

```json
{
  "layers": [
    {"id": "in",   "type": "input",   "in_features": 4},
    {"id": "h",    "type": "dense",   "out_features": 16},
    {"id": "a",    "type": "relu"},
    {"id": "drop", "type": "dropout", "p": 0.2},
    {"id": "out",  "type": "dense",   "out_features": 3}
  ],
  "connections": [["in","h"], ["h","a"], ["a","drop"], ["drop","out"]]
}
```

Datasets: a CSV whose last column is the integer class label (classification)
or continuous value (regression), or a pair of `X.npy` `(N,F)` + `y.npy`.

## Determinism design

Weight init, dropout and batch shuffling all draw from explicit, persisted RNG
streams seeded once per job. A checkpoint captures the model and optimizer
state together with the global torch RNG, a dedicated shuffle
`torch.Generator`, and a numpy generator. On resume all three are restored, so
the post-resume batch order and dropout masks are identical to a continuous
run. The DataLoader is single-process (`num_workers=0`) and shuffling uses the
persisted generator rather than process entropy.

## Tests

```bash
DATABASE_URL=postgresql+psycopg2://nn:nn@localhost:5432/nn_training_test \
  python -m pytest -q
```

Coverage includes:

- **shape / spec errors** — cycles, bad merges, unsupported layers, ranges;
- **concurrent claiming** — threads never double-assign a job (`SKIP LOCKED`);
- **per-user 3-job cap**, lease expiry/requeue, stale-executor rejection;
- **corrupt-checkpoint rollback** to the newest valid checkpoint;
- **resume == continuous** within tolerance, and no re-splitting;
- **pause/cancel races** at epoch boundaries (deterministic last-writer wins);
- **path traversal and symlink escape** rejection;
- **SSE replay / `Last-Event-ID`** and user isolation.

Threaded pause/cancel tests use a deterministic between-epoch interception hook
(`app.workers.runner.set_epoch_hook`) so they assert boundary semantics without
racing sub-millisecond CPU epochs; the hook is a no-op in production.
