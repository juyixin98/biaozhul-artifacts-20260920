# SIRCC — Security Incident Response Backend

A reference backend for managing security incidents through the full IR
lifecycle. Built with **Go + Chi + sqlc + PostgreSQL**. It is fully
self-contained: it talks to no real security devices or external services
(notifications are persisted rows, not pushed messages).

## What it does

- **Lifecycle:** `detected → triaged → contained → eradicated → recovered →
  postmortem → closed`, no skips.
- **Gates:** P1 needs an assigned responder before triage; closing needs a root
  cause, lessons learned, and an action item with an owner and due date (none
  left open).
- **Optimistic concurrency + idempotency:** transitions carry `expected_version`
  and `X-Request-Id`; duplicates replay the original result, stale versions are
  rejected. State, stage time, and audit rows commit in one transaction.
- **RBAC + case scope:** analysts triage and own evidence, responders run
  containment→recovery and close, admins assign people; users only see cases
  they belong to.
- **Evidence:** max 50 text items per case, immutable, corrections appended as
  notes; the cap is concurrency-safe. Export includes the stage timeline and an
  evidence summary.
- **Action-item reminders:** durable, exactly-once per due version; stale
  schedules after reschedule can't fire; restart catches up.
- **Honest durations:** containment/resolution times use only reached stages;
  unfinished records report `null`, never a fabricated end time.

## Quick start

```bash
make up          # postgres + api, migrations run on boot
make demo        # full HTTP walkthrough (needs curl + jq)
```

API: http://localhost:8080 · health: `GET /healthz`.
Three seeded users are available via `X-User-Id`: `u-analyst`,
`u-responder`, `u-admin` (see `migrations/0002_seed.up.sql`).

<details>
<summary>Manual example</summary>

```bash
curl -s localhost:8080/v1/incidents \
  -H 'X-User-Id: u-analyst' -H 'X-Request-Id: demo-1' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Phishing wave","severity":"P1"}'
```
</details>

## Layout

```
cmd/server            entrypoint (connect, migrate, serve, scheduler)
internal/db           sqlc-generated query layer + migration runner
internal/db/query     hand-written sqlc queries
internal/domain       stage order and role/transition rules
internal/service      business logic, transactions, reminder scheduler
internal/httpapi      Chi router and HTTP middleware
internal/middleware   X-User-Id authentication
migrations            schema + seed
test/integration      end-to-end tests against a real PostgreSQL
examples/demo.sh      scripted walkthrough
docs/api.md           full interface documentation
```

## Configuration

| Env var | Default | |
|---|---|---|
| `HTTP_ADDR` | `:8080` | listen address |
| `DATABASE_URL` | `postgres://sircc:sircc@localhost:5432/sircc?sslmode=disable` | |
| `MIGRATIONS_DIR` | `migrations` | |
| `REMINDER_INTERVAL` | `10s` | scheduler tick |
| `REMINDER_BATCH_SIZE` | `100` | max items per tick |

## Development

Requires Go 1.25, Docker (for Postgres), and [sqlc](https://sqlc.dev).

```bash
sqlc generate                 # regenerate internal/db from queries
go build ./...
go vet ./...

# tests (point at any empty-ish Postgres superuser URL):
docker run -d --name sircc-pg -e POSTGRES_USER=sircc -e POSTGRES_PASSWORD=sircc \
  -e POSTGRES_DB=postgres -p 5432:5432 postgres:16-alpine
make test-int                 # creates/drops a throwaway sircc_test_* database
```

The test suite covers transition races, close gates, evidence capacity
(sequential and concurrent), reschedule races, duplicate/idempotent requests,
reminder dedupe, restart catch-up, and authorization (越权) cases.

See [`docs/api.md`](docs/api.md) for the full interface contract.
