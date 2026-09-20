# SignalBoard

Publishing and synchronization API for **digital menu boards** in a chain of
restaurants. SignalBoard manages stores, an editable dish catalog, immutable
published menu versions, temporary ("happy hour") prices, idempotent sales
ingestion with automatic sold-out behavior, and the screen-facing read API that
physical displays poll.

There is intentionally **no frontend, no kitchen queue, and no analytics
dashboard**. The only external dependency is **PostgreSQL**.

- **Language/runtime:** Go 1.22, static binary (`CGO_ENABLED=0`, embedded
  migrations + embedded IANA tzdata).
- **Router:** [chi](https://github.com/go-chi/chi/v5).
- **Queries:** [sqlc](https://docs.sqlc.dev) generating type-safe code on top
  of [pgx/v5](https://github.com/jackc/pgx).
- **Database:** PostgreSQL 16 (uses `btree_gist` + an exclusion constraint).
- **Packaging:** multi-stage Dockerfile → distroless static image, plus Docker
  Compose for the app and database.

## Quick start (Docker Compose)

```bash
# start postgres + app (the server applies migrations on boot)
docker compose up --build

# in another shell, load demo data once
SEED=1 docker compose up app   # or run the seed binary / container
```

Then:

```bash
# screen menu (demo token printed by the seed)
curl -H 'X-Screen-Token: sbscr_demo_screen_token_change_me_0001' \
  http://localhost:8080/v1/screen/menu
```

## Run locally

```bash
# 1. start postgres (any way you like), e.g.
docker run --name sb-db -p 5432:5432 \
  -e POSTGRES_USER=signalboard -e POSTGRES_PASSWORD=signalboard \
  -e POSTGRES_DB=signalboard postgres:16-alpine

# 2. build and run (migration is automatic)
make build
DATABASE_URL='postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable' ./bin/server

# 3. optional demo data
DATABASE_URL='...' ./bin/seed
```

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ADDR` | `:8080` | Listen address |
| `DATABASE_URL` | local pgx URL | PostgreSQL connection string |
| `ADMIN_TOKEN` | `dev-admin-token` | Bearer token for admin/ingest endpoints |
| `SCREEN_OFFLINE_AFTER` | `90s` | Heartbeat age after which a screen is offline |
| `SHUTDOWN_TIMEOUT` | `15s` | Graceful HTTP shutdown budget |

## Core behavior & guarantees

- **Draft vs live.** Dishes are an editable catalog. Publishing snapshots the
  active dishes into an **immutable version**; later edits never change a
  published version and never affect screens until republished.
- **Optimistic, single-winner publishing.** Publish requires
  `expected_version` = current `menu_version`. The store row is locked
  `FOR UPDATE`, so concurrent publishes of the same version serialize — exactly
  one succeeds, the rest get `409`.
- **Batch import.** Up to **500** items per request, validated as a whole and
  applied in one transaction; one bad item rolls back the entire batch.
- **Temporary prices.** Integer cents; attached to the publish that introduces
  them; windows carry an explicit UTC offset, are half-open `[start, end)`, and
  must not overlap. Screens choose the in-effect price at read time, so a
  switch happens automatically at the boundary with **no republish**. A GiST
  exclusion constraint makes overlap impossible even under concurrent writes.
- **Sales ingest.** `(store_id, event_id)` is the idempotency key; a duplicate
  replay counts once, the same ID with different content is a `409`. Counters
  update atomically (no lost/double counts under concurrency). Events are
  bucketed by the **store's timezone day of `occurred_at`**, so late events
  land on the day they actually happened. Reaching a SKU's daily limit marks it
  sold-out for that local day; it **recovers automatically** the next day.
- **Screens.** Independent per-screen tokens scoped to one store. Reads use
  `ETag`/`If-None-Match` (`304` when unchanged) and are served from a single
  repeatable-read snapshot, so no response mixes old and new state. A publish,
  price-boundary crossing, sold-out change, or new day all invalidate the ETag.
  No heartbeat for 90s ⇒ offline; reconnect heartbeats and pulls the full
  latest menu.

## Project layout

```
cmd/server/              HTTP server entrypoint (static binary)
cmd/seed/                idempotent demo-data loader
internal/api/            chi handlers, auth, ETag/conditional logic (+ tests)
internal/config/         env configuration
internal/db/             sqlc-generated (DO NOT EDIT)
internal/httpx/          JSON/error helpers
internal/pgdb/           pool, embedded migrations, runner
db/query/                sqlc queries (*.sql)
internal/pgdb/migrations/versioned schema (*.sql, embedded)
API.md                   full endpoint reference
docker-compose.yml       app + postgres
Dockerfile               static build → distroless
```

## Regenerate sqlc

```bash
go install github.com/sqlc-dev/sqlc/cmd/sqlc@v1.27.0
make sqlc
```

## Tests

Tests are integration tests that exercise the full HTTP stack against a real
PostgreSQL (the concurrency and overlap guarantees cannot be proven against a
mock). They skip automatically if no database is reachable.

```bash
# point the suite at a database and run (also under -race)
TEST_DATABASE_URL='postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable' \
  make test
TEST_DATABASE_URL='...' make race
```

Coverage includes: concurrent single-winner publish, whole-batch import
rollback and the 500-item cap, half-open price boundaries + UTC offsets +
concurrent overlap enforcement, sales idempotency/conflict, concurrent
no-loss/no-double counting, threshold sell-out + next-day recovery, late-event
day attribution, screen token isolation, ETag/304 invalidation on
publish/sold-out/new-day, and the 90-second offline window.

See [`API.md`](./API.md) for the endpoint reference.
