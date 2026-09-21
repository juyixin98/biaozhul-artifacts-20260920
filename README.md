# SIRCC — Security Incident Response Coordination Center

Go + Chi + sqlc + PostgreSQL backend for the security incident response
lifecycle. No real security devices or external services are contacted;
authentication is header-based and reminders are persisted in PostgreSQL.

## Lifecycle

```
detected → triaged → contained → eradicated → recovered → postmortem → closed
```

- P1 incidents cannot finish triage without an assigned responder.
- Closing requires a root cause, lessons learned, and at least one action
  item with an owner and a due date.
- Transitions carry `expected_version` (optimistic concurrency) and
  `request_id` (idempotency: replays return the original result, stale
  versions are rejected). Status, phase records and audit events commit in a
  single transaction — no phase skipping, no half-updates.
- Roles: analysts (triage, evidence), responders (handling phases, action
  items), admins (personnel assignment). Every mutating endpoint checks both
  role and case assignment.
- Evidence: max 50 text entries per case (enforced by an atomic conditional
  counter — concurrency-safe), immutable once submitted; corrections are
  append-only linked notes. Export includes phase records and an evidence
  summary.
- Action items: due items produce persistent reminders, exactly once per
  `due_version`. Rescheduling bumps `due_version` so stale schedules never
  fire; the DB-driven worker catches up after restarts.
- Durations (time-to-contain / time-to-resolve) are computed only from valid
  phase timestamps; incomplete phases yield `null`, never a fabricated end.

## Quick start (Docker)

```sh
docker compose up --build        # db + app on :8080
./samples/demo.sh                # end-to-end walkthrough
docker compose --profile test run --rm test   # integration tests
```

Host ports can be remapped via `SIRCC_PORT` / `SIRCC_DB_PORT`.

## Local development

```sh
# start a postgres, then:
export DATABASE_URL=postgres://sircc:postgres@localhost:5432/sircc?sslmode=disable
make run        # migrations run automatically at startup
make test       # integration tests (skip without DATABASE_URL)
make sqlc       # regenerate internal/db from db/queries (needs sqlc)
```

## Layout

- `cmd/server` — entrypoint (migrations, reminder worker, HTTP server)
- `internal/migrations` — SQL migrations (embedded, applied at startup)
- `db/queries` — sqlc query definitions → `internal/db` (generated)
- `internal/incident` — lifecycle, gates, evidence, action items, export
- `internal/reminder` — due-action-item reminder worker
- `internal/httpapi` — chi router, header auth, JSON error mapping
- `internal/httpapi/api_test.go` — integration tests: transition races,
  idempotent replay, close gates, P1 triage gate, evidence capacity under
  concurrency, reschedule races, duplicate scheduling, authorization/case
  scope, phase skipping, duration correctness
- `docs/api.md` — API reference
- `samples/demo.sh` — scripted end-to-end demo
