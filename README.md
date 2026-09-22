# DAMS — Database Activity Audit Backend

A backend that collects **locally-produced test events** describing database
activity, detects suspicious patterns, manages the resulting alerts through a
review workflow, and records every administrative/alert action in a
tamper-evident hash chain.

> Scope: DAMS **never connects to a monitored database** and **never executes
> uploaded SQL**. It only ingests JSON event batches posted to its HTTP API.

Built with **Go 1.22**, **Chi**, **sqlc** (generated, type-safe SQL), and
**PostgreSQL 16**.

## What it does

### 1. Batched ingestion (`POST /api/v1/orgs/{org}/events:batch`, admin)

- At most **2000 events per batch**; each event carries source database,
  user, event time, operation category, schema/table and row count.
- **Deduplication by `(source, event_id)`**:
  - same id + same content → counted once, returned as `duplicates`;
  - same id + different content → **HTTP 409 `id_conflict`** and the *whole*
    batch rolls back;
  - concurrent identical retries can never double-count (UNIQUE constraint +
    transaction + retry of deadlocks/serialization failures).
- Content fingerprint is a SHA-256 of the canonical payload (time reduced to
  UTC nanoseconds, so equivalent timestamps hash equally).

### 2. Detection (runs in the same ingest transaction)

Rules are **versioned**; editing a rule inserts a new version and existing
alerts stay bound to the version that fired them.

- **Rate rule** — *more than N events by the same user within W seconds*.
  Windows are **fixed, epoch-aligned, half-open**:
  `[windowStart, windowStart + W)`; an event exactly on the closing boundary
  belongs to the next window. Defaults N=500, W=300.
- **Sensitive-table rule** — access outside allowed local hours in the
  organization's IANA timezone. The interval is **`[hour_start, hour_end)`**:
  06:00:00 is allowed, **20:00:00 is not**, 19:59:59 is allowed.
- **Late / out-of-order events**: each ingest recomputes every fixed window
  and table/event it touches from the events table, so late events append
  evidence to existing alerts. Alerts carry a stable fingerprint
  (`rate:<rule>:v<version>:<user>:<windowStart>`,
  `sensitive:<rule>:v<version>:event:<pk>`) backed by a UNIQUE constraint, so
  an alert is **never produced twice**.

### 3. Alert lifecycle

States: `open → investigating → resolved | false_positive` (terminal),
plus direct `open → resolved/false_positive`. Every change requires the
caller's **`expected_version`** (optimistic concurrency) and appends an
immutable revision (`created | transition | assign | note | recompute`).

Recomputation (ingest-driven or `POST …/alerts/{id}:recompute`) **only
appends evidence** (event links, counter, `recompute` revision). It never
changes status, assignment, or investigator notes.

### 4. Append-only audit hash chain

All rule changes and alert actions append to a per-organization chain with a
gapless `seq` starting at 1, a SHA-256 of canonical JSON content, the
previous entry's hash, and a covering entry hash:

```
content_hash = sha256(canonical_json(content))
entry_hash   = sha256(seq | org | action | content_hash | prev_hash)
```

Concurrent appenders take a per-org `pg_advisory_xact_lock`, so the chain
cannot fork. `GET /api/v1/orgs/{org}/audit/verify` reports
**gaps (missing entries), tampered content, broken links (rewrite/out-of-order
reinsertion), and invalid entry hashes**. Database triggers reject UPDATE/DELETE
on `audit_entries` and `alert_revisions` under normal connections.

### 5. RBAC and export

| Role    | Ingest / configure rules | Work alerts | Read + export | Audit chain |
|---------|--------------------------|-------------|---------------|-------------|
| admin   | ✅                        | ✅           | ✅             | ✅           |
| analyst | ❌ (403)                  | assigned org only | ✅       | ❌ (403)     |
| auditor | ❌ (403)                  | ❌ (403)     | ✅ read-only   | ✅           |

Cross-organization users cannot see or act on another org's resources (403).
Exports (`JSONL` streamed or `CSV`) apply the org mask policy to every role;
default masked fields are `db_user` and `table_name`. Every export is itself
recorded in the audit chain.

## Quick start (Docker)

```bash
docker compose up -d --build                 # postgres + API on :18080
docker compose run --rm seed                 # demo org + users, prints tokens
# or pick a different host port:
DAMS_HTTP_PORT=28080 docker compose up -d
```

Then:

```bash
TOKEN=...        # from the seed output, role=admin org=demo
curl -s localhost:18080/healthz
curl -s -X POST localhost:18080/api/v1/orgs/demo/events:batch \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @samples/events-batch.json
```

Seeded users: `ada@dams.local` (admin), `ana@dams.local` (analyst),
`audra@dams.local` (auditor) in org `demo` (timezone `Asia/Shanghai`), plus a
separate `other` org for cross-auth tests.

## Local development

```bash
go run ./cmd/damsctl migrate          # apply embedded migrations
go run ./cmd/damsserver               # serve (DAMS_DATABASE_URL, DAMS_HTTP_ADDR)
```

Regenerate sqlc code after changing `internal/db/queries/*.sql`:

```bash
(cd internal/db && sqlc generate -f sqlc.yaml)
```

## Tests

Integration tests use a real PostgreSQL and cover: whole-batch rollback on
conflict, in-batch/sequential/concurrent dedup, 2000 cap, fixed-window
boundaries, late out-of-order recomputation, rule-version rebinding,
org-timezone sensitive-table boundaries, optimistic alert transitions
(including concurrent decisions), verdict preservation across recompute,
concurrent gap-free audit appends, chain tamper/gap/out-of-order detection,
append-only triggers, cross-org RBAC and masked export.

```bash
./scripts/run-tests.sh                      # disposable docker postgres
./scripts/run-tests.sh -race -count=1
# or against your own database:
DAMS_TEST_DATABASE_URL=postgres://… go test ./...
```

## Layout

```
cmd/damsserver/        HTTP API entrypoint
cmd/damsctl/           migrate + seed
internal/migrate/      embedded SQL migrations
internal/db/queries/   sqlc queries    internal/db/sqlc/  generated code
internal/detection/    window/timezone detection + alert reconciliation
internal/auditchain/   canonical hashing, serialized append, verification
internal/auth/         bearer tokens, principal, roles
internal/server/       chi router, handlers, RBAC, masked export
samples/               example batch + rule payloads
scripts/run-tests.sh   disposable-Postgres test runner
```

## HTTP surface

| Method & path | Role |
|---|---|
| `GET  /healthz` | none |
| `GET  /api/v1/orgs` | any member (own orgs only) |
| `POST /api/v1/orgs/{org}/events:batch` | admin |
| `GET  /api/v1/orgs/{org}/sources` | admin |
| `GET  /api/v1/orgs/{org}/rules` · `…/rules/{id}/versions` | member |
| `POST /api/v1/orgs/{org}/rules` | admin |
| `GET  /api/v1/orgs/{org}/alerts` · `…/alerts/{id}` · `…/revisions` | member |
| `POST …/alerts/{id}:transition` · `:assign` · `:recompute` | admin, analyst |
| `GET  /api/v1/orgs/{org}/audit` · `…/audit/verify` | admin, auditor |
| `GET  /api/v1/orgs/{org}/events:export?format=jsonl|csv` | member (masked) |
| `PUT  /api/v1/orgs/{org}/export-policy` | admin |
