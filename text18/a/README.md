# SIRCC — Security Incident Response Backend

A self-contained backend for managing security incidents through a strict
response lifecycle. Built with **Go + Chi + sqlc + PostgreSQL**. It does not
connect to any real security device or external service; authentication uses
static seeded API keys and overdue notifications are durable database rows
(a scheduler logs them), so it is safe to run in an isolated environment.

## Lifecycle

Every incident moves forward through exactly these phases — phases can never
be skipped or reordered:

```
detected → triaged → contained → eradicated → recovered → reviewed → closed
```

- **P1 cannot be triaged** until an admin has assigned at least one responder
  to the case.
- **Closure gate**: before `close` the case must have a **root cause**,
  **lessons learned**, and at least one **action item with an owner and a
  deadline** (canceled items do not count).
- Each transition carries an **`expected_version`** (optimistic concurrency)
  and an optional idempotency **request id**. A stale version is rejected with
  `409`; a repeated request id replays the original stored response.
- The incident status, the new phase timestamp and the audit event are written
  in one transaction — there is no observable half-update.

## Roles and case scope

| Role       | Can do                                                                 |
|------------|------------------------------------------------------------------------|
| admin      | Everything, across all cases; assigns people; closes cases.           |
| analyst    | Creates cases, performs triage/review, adds evidence & corrections.   |
| responder  | Drives containment, eradication, recovery on assigned cases.          |

Non-admins may only touch cases they are a **member** of. Membership is
per-case; an admin grants it.

## Evidence

- At most **50 text evidence items per case**. Inserts serialize on a row lock,
  so concurrent submits can never bypass the cap.
- Evidence is **append-only and immutable**. A correction is a linked
  **note**; the original body is never overwritten.
- Case export includes ordered phase records, derived metrics and an evidence
  summary (with correction-note counts).

## Action items and reminders

- Each action item has an owner and a deadline. Rescheduling atomically bumps a
  **`due_version`**.
- A background sweep persists one reminder row per `(item, due_version)`:
  - the same due version is reminded **at most once**;
  - after a reschedule the **old schedule can never emit a stale reminder**;
  - reminders are durable, so on restart the first sweep **catches up** on
    every deadline missed while the process was down.

## Metrics

Containment/resolution/closure times are derived **only** from recorded phase
entry timestamps. An interval whose ending phase has not been entered is
`null` — unfinished work is never assigned a fabricated end time.

## Quickstart

### Docker (recommended)

```bash
docker compose up --build
# API on http://localhost:8080, PostgreSQL on localhost:5432
```

Migrations run automatically at startup (and are embedded in the image). To run
them as a one-shot job:

```bash
docker compose --profile migrate run --rm migrate
```

### Local

Requires Go 1.22+, a reachable PostgreSQL, and `sqlc` only if you regenerate
code.

```bash
createdb sircc            # or point SIRCC_DATABASE_URL anywhere
make run                   # applies embedded migrations, then serves :8080
```

## Demo users / API keys

Seeded by the first migration (demonstration only — keys are SHA-256 hashed):

| Username    | Role      | API key                       |
|-------------|-----------|-------------------------------|
| admin       | admin     | `sircc_demo_admin_0001`       |
| analyst1    | analyst   | `sircc_demo_analyst_0001`     |
| analyst2    | analyst   | `sircc_demo_analyst_0002`     |
| responder1  | responder | `sircc_demo_responder_0001`   |
| responder2  | responder | `sircc_demo_responder_0002`   |

Send the key as `X-API-Key: <key>` or `Authorization: Bearer <key>`.

```bash
curl -s localhost:8080/healthz
curl -s -H 'X-API-Key: sircc_demo_analyst_0001' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Suspicious login spike","severity":"P2"}' \
  localhost:8080/api/v1/incidents
```

A full worked walkthrough lives in [`examples/walkthrough.sh`](examples/walkthrough.sh).
The endpoint reference is in [`docs/api.md`](docs/api.md).

## Configuration

| Env var                      | Default                                              | Meaning                          |
|------------------------------|------------------------------------------------------|----------------------------------|
| `SIRCC_HTTP_ADDR`            | `:8080`                                              | Listen address.                  |
| `SIRCC_DATABASE_URL`         | `postgres://sircc:sircc@localhost:5432/sircc?sslmode=disable` | PostgreSQL DSN.        |
| `SIRCC_SCHEDULER_INTERVAL`   | `10s`                                                | Overdue-reminder sweep interval. |

## Development

```bash
make sqlc       # regenerate internal/store from internal/database/queries
make test       # integration tests (needs sircc_test database)
make test-race  # same with the race detector
make vet
```

The integration suite resets the schema in `sircc_test` before every test:

```bash
sudo -u postgres psql -c "CREATE ROLE sircc LOGIN PASSWORD 'sircc' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE sircc_test OWNER sircc;"
make test
```

## Project layout

```
cmd/sircc/                 entrypoint (migrate flag, HTTP server, scheduler)
internal/api/              Chi router and HTTP handlers
internal/auth/             API-key authentication middleware
internal/config/           environment configuration
internal/clock/            real and mock time source
internal/database/         embedded SQL migrations + migration runner
internal/database/queries/ sqlc queries (*.sql)
internal/domain/           lifecycle constants, roles and domain errors
internal/service/          business logic: transitions, evidence, reminders, export
internal/store/            sqlc-generated data access (do not edit)
internal/testsupport/      test DB pool and schema reset helpers
tests/integration/         end-to-end tests incl. concurrency & RBAC
docs/, examples/           API reference and a worked curl walkthrough
```
