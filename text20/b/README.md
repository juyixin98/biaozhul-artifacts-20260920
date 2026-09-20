# SignalBoard

Publishing &amp; synchronization service for **digital menu boards in restaurant
chains**. Manage stores/dishes/drafts, publish immutable menu versions,
schedule time-banded prices, ingest point-of-sale events with automatic
sellout, and let screens pull one consistent, cache-safe view.

- **Stack:** Go 1.22, [Chi](https://github.com/go-chi/chi) router,
  [sqlc](https://sqlc.dev)-generated queries, PostgreSQL 16, [pgx](https://github.com/jackc/pgx) v5.
- Single **static binary** (CGO disabled); the only external dependency is
  PostgreSQL. No frontend, no kitchen queueing, no analytics dashboard.

## Guarantees at a glance

| Concern | How it is enforced |
|---|---|
| Drafts don't touch live | Separate draft tables; publishing copies an immutable snapshot |
| Immutable versions | `menu_versions` / `menu_version_items` are insert-only; old versions still readable |
| One winner on concurrent publish | Per-store row lock + `expected_version` compare inside one tx → 412 for losers |
| Batch import is all-or-nothing | Single transaction, max 500 items, any validation error rolls the batch back |
| Temp-price windows | Half-open `[start,end)`, overlap rejected in app **and** by a GiST `EXCLUDE` constraint |
| Auto price switching | Screen read evaluates the active window at read time — no republish/cron |
| Exactly-once sales | Unique `(store_id,event_id)`; duplicate id + different payload → 409 |
| No lost/doubled concurrent counts | Insert + atomic upsert inside one transaction |
| Store-local daily totals / sellout | Day bucket computed in the store's IANA zone; new day restores sales |
| Late events | Attributed to the store-local day of `occurred_at`, not arrival time |
| Screen isolation | Per-screen hashed token; endpoint has no store parameter |
| No stale cache after change | Strong `ETag` over version + effective prices + sellout flags; `If-None-Match`/`If-Match` |
| No mixed new/old state | Menu assembled in one repeatable-read snapshot |
| Offline detection | 90 s heartbeat threshold; reconnect re-pulls the full current menu |

## Layout

```
cmd/server/         entrypoint (serve / migrate / seed)
internal/db/        sqlc-generated code (do not edit)
sqlc/               hand-written SQL queries
migrations/         canonical schema migrations
internal/migrate/   embedded migration runner (migrations embedded in binary)
internal/service/   business logic, transactions, concurrency control
internal/httpapi/   Chi handlers, auth, conditional requests
internal/seed/      demo data
internal/testsupport/  per-test PostgreSQL database provisioning
docs/api.md         full HTTP API reference
```

## Quick start with Docker Compose

```bash
docker compose up -d db            # PostgreSQL
docker compose up --build app      # applies migrations, serves :8080
docker compose run --rm seed       # optional demo data
```

Environment: `DATABASE_URL`, `ADDR` (default `:8080`),
`MANAGEMENT_API_KEY` (default `dev-management-key` — override in real use).

## Run without Docker

```bash
createdb signalboard
export DATABASE_URL='postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable'
go build -o signalboard ./cmd/server
./signalboard migrate   # idempotent; also applied automatically on boot
./signalboard seed      # optional demo data
./signalboard           # serve
```

## Regenerate database code

SQL is the source of truth. After editing `sqlc/*.sql` or the schema:

```bash
sqlc generate
```

Migrations are embedded from `internal/migrate/sql/`; keep them identical to
`migrations/` (the Docker build copies them; locally you can copy by hand).

## Tests

Integration tests spin up a unique migrated database per test and need a
PostgreSQL role that can create databases:

```bash
# default admin DSN: postgres://postgres@localhost:5432/postgres?sslmode=disable
go test ./...

# e.g. via the local peer socket as the postgres OS user:
TEST_DATABASE_ADMIN_URL='postgres://postgres@/postgres?host=/var/run/postgresql&sslmode=disable' \
  go test -race -count=1 ./...
```

Coverage includes: 12-way concurrent publish, batch rollback, half-open price
boundaries (including nanosecond edges), DB-level concurrent overlap
rejection, idempotent/conflicting/concurrent sales, threshold sellout and
next-day recovery, late-event day attribution, timezone bucketing, ETag
invalidation on publish/price/sellout, and screen store isolation.

See [`docs/api.md`](docs/api.md) for the API reference.
