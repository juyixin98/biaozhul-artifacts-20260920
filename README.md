# SignalBoard

Publish & sync API for digital menu boards in restaurant chains.
Go + Chi + sqlc + PostgreSQL, compiled to a fully static binary, deployed with
Docker Compose. PostgreSQL is the only external dependency — no frontend, no
kitchen queue, no analytics dashboard.

## Quick start

```bash
docker compose up --build        # db on :15432, api on :8080 (override with DB_PORT/APP_PORT)
docker compose logs app          # prints the demo screen token (SEED_DEMO=true)
curl -H "Authorization: Bearer <token>" http://localhost:8080/screen/menu
```

Local development (needs a PostgreSQL and `DATABASE_URL`):

```bash
createdb signalboard
DATABASE_URL=postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable \
SEED_DEMO=true go run ./cmd/signalboard
```

Migrations in `internal/migrate/migrations/` are embedded in the binary and
applied automatically at startup. `sqlc generate` regenerates `internal/db`
from `queries/` + the migrations (verified byte-for-byte).

## Tests

Integration tests run against a real PostgreSQL:

```bash
createdb signalboard_test
go test ./...            # or TEST_DATABASE_URL=... go test ./...
```

Covered: concurrent publish (exactly one winner), optimistic version
conflicts, draft/live isolation, version immutability, import rollback and the
500-item limit, half-open temp-price bands (adjacent allowed, overlap
rejected, concurrent overlap serialized), effective-price windows, sales
idempotency (duplicate / conflict / concurrent duplicates counted once),
late events attributed to the occurred day, sold-out recovery across days,
screen auth & store isolation, ETag invalidation on publish/price/sold-out,
and the 90-second heartbeat online rule.

## Design notes

- **Money** is integer cents (`price_cents`). **Time** is RFC-3339 with an
  explicit UTC offset; everything is stored as `timestamptz`.
- **Drafts vs. versions**: each store has one mutable draft. Publishing
  copies the draft into an immutable `menu_versions` snapshot; drafts never
  affect the live menu.
- **Optimistic publish**: `POST /publish` carries `expected_version`. The
  version is claimed with a single conditional `UPDATE ... WHERE
  current_version = $expected`, so exactly one of N concurrent publishers
  wins; the rest get `409`.
- **Temp prices** are half-open bands `[starts_at, ends_at)` published with
  the menu. Overlap is forbidden by a PostgreSQL exclusion constraint
  (`tstzrange ... WITH &&`), which concurrent writers cannot bypass. The live
  price switches automatically at band boundaries — no republish needed.
- **Sales events** are idempotent on the client-supplied event id (the PK):
  same id + same content → `200 duplicate`, same id + different content →
  `409`. Insert and daily-aggregate update happen in one transaction, so
  concurrent duplicates are counted exactly once. The daily bucket is the
  store-local day of `occurred_at`, so late events land on their real day and
  sold-out resets automatically on a new day.
- **Screens** authenticate with a per-screen bearer token (only its SHA-256
  is stored) and can only read their own store. `GET /screen/menu` is one SQL
  query — one database snapshot, never a mix of old and new state — and
  carries an ETag over the visible state, so any publish/price/sold-out
  change invalidates caches. A screen is `online` while heartbeats arrive
  within 90 seconds; on reconnect it simply fetches the full menu again.

See [docs/API.md](docs/API.md) for the endpoint reference.
