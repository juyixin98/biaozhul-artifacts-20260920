# DAMS — Database Activity Monitoring & Audit backend

DAMS ingests **local test events** describing database activity, detects
risky patterns, manages alerts through a triage lifecycle, and records every
administrative/alert action in a tamper-evident audit chain.

It is a receiver/analyzer only: it **never connects to a monitored database**
and **never executes SQL carried in events** (`sql_text` is stored as opaque
evidence and masked on export).

Stack: **Go 1.22 · chi · sqlc · PostgreSQL 16 · Docker Compose**.

---

## 1. Quick start (Docker)

```bash
docker compose up -d --build
# wait for health, then seed one org with four role keys + default rules:
docker compose --profile seed run --rm seed
```

The seed prints keys once (only SHA-256 hashes are stored):

```
organization: acme (id=1, tz=Asia/Shanghai)
new API keys (store now, only hashes are retained):
  admin      dams_admin_<hex>
  analyst    dams_analyst_<hex>
  auditor    dams_auditor_<hex>
  collector  dams_collector_<hex>
```

Server: `http://localhost:8080`, health at `GET /healthz`. Migrations apply
automatically on boot (`DAMS_AUTOMIGRATE=true`).

### Local (without Docker)

```bash
# any Postgres 16, e.g.
docker run -d --name dams-db -p 5432:5432 \
  -e POSTGRES_USER=dams -e POSTGRES_PASSWORD=dams -e POSTGRES_DB=dams postgres:16-alpine
export DAMS_DATABASE_URL='postgres://dams:dams@localhost:5432/dams?sslmode=disable'
go run ./cmd/migrate
go run ./cmd/seed --org acme --tz Asia/Shanghai   # save the printed keys
go run ./cmd/server
```

Regenerate sqlc after editing queries: `sqlc generate` (v1.25).

---

## 2. Event model & batching

`POST /v1/events:batch` (roles: **collector, admin**), max **2000** events/batch:

```json
{
  "source": "db-prod-01",
  "events": [{
    "source_event_id": "pgbin-000001",
    "db_user": "app_svc",
    "occurred_at": "2026-03-10T09:12:31Z",
    "action_category": "select",
    "schema_name": "public",
    "table_name": "orders",
    "row_count": 50,
    "client_ip": "10.0.3.14",
    "sql_text": "SELECT ..."
  }]
}
```

`action_category ∈ select|insert|update|delete|ddl|grant|login|other`.

Response:

```json
{"batch_id":"…","received":2,"inserted":2,"duplicates":0,"alerts_created":0}
```

### Dedup, conflict, atomicity

- Identity is `(org, source, source_event_id)` (unique index).
- Content is `sha256(canonical JSON of the semantic fields)`.
- **Same id, same content** → duplicate replay: counted in `duplicates`,
  **never re-inserted, never re-detected**. Detection runs only over genuinely
  new rows.
- **Same id, different content** → `409 conflict` and **the whole batch rolls
  back** — every row of that batch (including unrelated new IDs) is discarded.
- Any other error likewise rolls the batch back (one transaction wraps
  validation inserts + detection).
- **Concurrent retries** are serialized by the unique index; the loser of the
  race re-reads the committed row, classifies it as duplicate or conflict, and
  never double-counts.

---

## 3. Detection rules & window semantics

Rules are **versioned** (`/v1/rules/`, admin only). Alerts bind to the exact
version effective at the window/event time, so changing a rule never rewrites
history. At most one version per `(org, rule_type)` is active (partial unique
index; creation deactivates predecessors in the same transaction).

### 3.1 Frequency — >500 actions by one user within 5 minutes

`{"window_seconds":300,"threshold":500,"actions":[...]}` (`actions` omitted =
all categories).

- Windows are evaluated on a fixed **1-minute step grid**, truncated in the
  **organization timezone**. Each event at `t` belongs to five stepped windows
  `[t0−k·1m, t0−k·1m+5m)` for `k=0..4` (where `t0` is t truncated to the
  minute). A burst therefore raises at most one alert per affected window and
  the same burst can violate five consecutive windows.
- **Boundary: half-open `[start, end)`.** An event whose time equals
  `window_end` is **not** counted in that window (it belongs to the next).
- Counts are always recomputed from the authoritative `events` table.
- Alert fingerprint: `freq:<rule_version_id>:<db_user>:<window_start>`, so a
  window produces exactly one alert regardless of how many times it is
  recomputed.

**Late / out-of-order events** trigger recomputation of every stepped window
that contains them. Recomputation upserts window state; if the fingerprinted
alert already exists it is **not** duplicated — instead an **evidence
revision is appended** when the count changed (with the full contributing
event set relinked), leaving the investigator's status untouched.

### 3.2 Sensitive tables outside business hours

```json
{"sensitive_tables":[{"schema":"public","table":"salaries"}],
 "allowed_start_hour":6,"allowed_end_hour":20,"actions":["select"]}
```

- Evaluated in the **organization timezone**.
- **Boundary: half-open `[06:00, 20:00)`** — access at `06:00:00` is allowed;
  access at `20:00:00` sharp is already a violation; `05:59:59` is a violation.
- One alert per event: fingerprint `sens:<rule_version_id>:<event_id>`.

---

## 4. Alert triage

Statuses: `pending → investigating → resolved | false_positive` (resolved/false
positive may be reopened to `investigating`).

- `POST /v1/alerts/{id}:transition` body
  `{"expected_version":N,"status":"investigating","note":"…"}`.
- **Optimistic concurrency**: the transition succeeds only when
  `expected_version` equals the stored `version`; otherwise `409`. Each
  successful transition increments `version`.
- Every transition appends an immutable row to `alert_status_history`
  (from/to/note/actor/timestamp) and an `alert.update` entry to the audit
  chain.
- Machine **evidence revisions are append-only**. Recomputation can add a
  revision/relink events but cannot change status, version, or resolution.
- `POST /v1/alerts/recompute {"db_user":"…","window_start":"…"}` manually
  recounts one window (analyst/admin) and is itself audited.
- `GET /v1/alerts/{id}` returns the alert, status history, evidence revisions,
  and contributing (masked) events.

---

## 5. Append-only audit chain

Rule changes (`rule.create`) and alert actions (`alert.update`,
`alert.recompute`) are written to a per-organization hash chain:

```
entry_hash = sha256( seq ‖ "\n" ‖ prev_hash ‖ "\n" ‖ sha256(canonical JSON payload) )
```

- `seq` is a dense per-organization sequence; `prev_hash` of entry 1 is the
  64-zero hash.
- Append holds a **transaction-scoped advisory lock keyed by org** and updates
  head state in the same transaction → concurrent appenders serialize and the
  chain cannot fork or share a sequence number.
- `GET /v1/audit/entries` lists entries; `GET /v1/audit/verify` recomputes the
  entire chain and reports:
  - `gap` — missing sequence number (deleted entry),
  - `broken_link` — `prev_hash` does not match the predecessor,
  - `tampered` — recomputed `entry_hash` differs (payload modified),
  - `out_of_order` — duplicate/regressing sequence,
  - `head_mismatch` — stored head seq/hash disagrees with the recomputed tail
    (rewound or uncommitted fork).

  Verification returns `200 {"ok":true}` or `409 {"ok":false,"issues":[…]}`.

---

## 6. Roles & RBAC

Every API key is scoped to exactly one organization; all queries are filtered
by `org_id`, so an analyst can only ever see/triage their own org's alerts
(cross-org access returns `404`, not the object).

| Action | admin | analyst | auditor | collector |
|---|---|---|---|---|
| Ingest events | ✅ | ❌ | ❌ | ✅ |
| Configure rules | ✅ | ❌ | ❌ | ❌ |
| List/get alerts | ✅ | ✅ | ✅ | ❌ |
| Transition / recompute alerts | ✅ | ✅ | ❌ | ❌ |
| Export events (masked) | ✅ | ✅ | ✅ | ❌ |
| Read audit entries / verify | ✅ | ❌ | ✅ | ❌ |

`GET /v1/events` is the export surface for every reader role and always masks
sensitive fields: `sql_text` and `client_ip` render as `***MASKED***` (empty
stays empty); identity/time/table/row-count remain visible. The raw values are
kept in the database as evidence but never leave through export.

---

## 7. Project layout

```
cmd/server        HTTP service (auto-migrates)
cmd/migrate       standalone migration runner
cmd/seed          demo org + role keys + default rules
migrations -> internal/platform/migrate/sqlfiles   embedded SQL (0001..0005)
internal/db       sqlc-generated queries/models
internal/db/queries    hand-written sqlc queries
internal/platform auth (bearer/RBAC), httpx, dbpool, migrate, canonical JSON, hash
internal/service  ingest, detection, rules, alerts, ruleadmin, auditchain, export
internal/api      chi router, handlers, presenters
examples          sample payloads + smoke.sh
tests             integration tests (real Postgres)
```

---

## 8. Tests

Integration tests use a real PostgreSQL; set the DSN if needed
(default `postgres://dams:dams@localhost:55080/postgres?sslmode=disable`):

```bash
export TEST_DATABASE_URL='postgres://dams:dams@localhost:5432/dams?sslmode=disable'
go test ./...
```

Each test creates an isolated organization, so runs are order-independent and
can share one database. Coverage:

- **Batch rollback** — same-id/different-content conflict discards the whole
  batch; exact replay is idempotent.
- **Concurrent retries** — 12 goroutines resending a 40-event batch yield
  exactly 40 rows.
- **Out-of-order/late backfill** — affected windows recompute; counts
  `501 → 511`, evidence revision appended, no duplicate alerts.
- **Window boundaries** — half-open `[start,end)` for frequency; `[06,20)`
  half-open in org timezone for sensitive hours (05:59:59 and 20:00:00 alert;
  06:00:00 and 19:59:59 don't).
- **Alert recompute vs verdict** — late data appends evidence but a
  `false_positive` verdict and its version survive.
- **Optimistic locking & history** — stale `expected_version` → 409;
  lifecycle rows appended.
- **Audit chain** — 30 concurrent appends produce gapless seq 1..30;
  verification detects tampered payload, deleted entry, and rewound head.
- **RBAC / cross-org / masking** — full role matrix, org A cannot reach org B
  alerts, export masks sensitive fields for every role.

Pure unit tests cover window math, half-open sensitive hours (including
overnight windows), canonical JSON, and hash-chain sensitivity/propagation.
