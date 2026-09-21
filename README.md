# ClearSettle

ClearSettle is a **local simulated payment processing and reconciliation engine**.
There is no real payment gateway: captures and refunds are recorded against an
internal double-entry ledger and an independent simulated acquirer feed
(`channel_events`) used for reconciliation.

Built with **Go 1.22, Chi, sqlc, PostgreSQL 16, and Docker Compose**.

---

## 1. Quick start

```bash
# 1. Start PostgreSQL (host port 35432 -> container 5432)
docker compose up -d postgres

# 2. Run the API (applies migrations and bootstraps an admin automatically)
DATABASE_URL='postgres://clearsettle:clearsettle@127.0.0.1:35432/clearsettle?sslmode=disable' \
HTTP_ADDR=':8080' \
go run ./cmd/api
```

The first startup:

- applies every file in `db/migrations/` (tracked in `schema_migrations`);
- bootstraps an admin from `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`
  (defaults `admin@clearsettle.local` / `admin12345` — **change these**).

Health check:

```bash
curl -s localhost:8080/healthz
# {"status":"ok"}
```

### Running everything in containers

```bash
docker compose up -d --build          # postgres + api (in-process worker)
docker compose --profile split-worker up -d worker   # optional standalone worker
```

### Running the worker separately

```bash
WORKER_ENABLED=false go run ./cmd/api        # API only
go run ./cmd/worker                          # settlement + reconciliation loop
```

### Regenerating the sqlc store

```bash
docker run --rm -v "$PWD:/src" -w /src sqlc/sqlc:1.27.0 generate
```

---

## 2. Money and fee rules

- All amounts are **integer cents** (`BIGINT`). Floating point is never used.
- Default merchant fee is **2.9% + 30¢**:

  ```
  fee = round_half_up(amount * bps / 10000) + fixed
  ```

  with `bps = 290`, `fixed = 30`. The proportional term uses **round half up**
  (ties go away from zero). Example: $40.00 → `round(4000*290/10000)=116` + `30`
  = **146¢**.
- On a refund, a proportional share of the original fee is returned:

  ```
  fee_refund = round_half_up(original_fee * refund_amount / captured_amount)
  ```

  The **last refund** of a payment returns the exact remaining fee, so the
  cumulative returned fee equals the original fee with no rounding residue.
  Refunded fees can never exceed the fee charged.

---

## 3. Payment state machine

```
                 ┌─────────┐  within 24h   ┌────────┐
   authorize ──▶ │authorized│ ───────────▶ │ voided │   (terminal)
                 └────┬─────┘               └────────┘
                      │ capture (partial or full)
                      ▼
                 ┌─────────┐  refund    ┌────────────────────┐
                 │captured │ ─────────▶ │partially_refunded  │
                 └────┬────┘            └─────────┬──────────┘
                      │            refund up to captured amount
                      ▼                             ▼
                 ┌─────────┐              ┌────────────┐
                 │settled* │              │ refunded   │ (terminal)
                 └─────────┘              └────────────┘
```

Allowed transitions are enforced server-side and re-checked by the SQL `UPDATE`
predicates. `settled` payments accept further refunds within the refund window
(they post negative payable that nets into a future payout).

| Rule | Limit |
|---|---|
| Void / capture an authorization | within **24 hours** of authorization (`expires_at`) |
| Refund a captured payment | within **90 days** of capture |
| Cumulative refunds | **never exceed the captured (paid) amount** |
| Partial captures | allowed, up to the authorized amount |
| Partial refunds | allowed; a final full refund sets status `refunded` |

Illegal transitions return `409 illegal_transition` (or `422 refund_too_large`).

---

## 4. Double-entry ledger

`ledger_entries` is an **append-only** table:

- `BEFORE UPDATE/DELETE` triggers reject every modification — corrections are
  booked as **new reversing entries** (`ref_type='correction'`).
- Every posting (a group of lines sharing `ref_type` + `ref_id`) must net to
  zero; a deferred constraint trigger verifies this at transaction commit.
- Business postings (signed cents; positive is a debit in our convention):

  | Event | CASH | FEE_REVENUE | PAYABLE:&lt;merchant&gt; |
  |---|---:|---:|---:|
  | Capture of A, fee F | +A | −F | −(A−F) |
  | Refund of R, fee back G | −R | +G | +(R−G) |
  | Daily settlement net N | −N | | +N |

  Liability (`PAYABLE`) is stored negative, so a merchant's balance owed is
  `−SUM(payable entries)`. The three global accounts `CASH`, `FEE_REVENUE` and
  one `PAYABLE:<merchant>` per merchant are row-locked in deterministic order to
  serialize concurrent postings without deadlocks.

**Status, balances, and ledger entries always commit together in one database
transaction** — there is no window for a payment to be "captured" but unposted.

---

## 5. Idempotency and concurrency

Every money-mutating request (`authorize`, `capture`, `void`, `refund`) requires
an idempotency key, sent as the `Idempotency-Key` header or `idempotency_key`
JSON field. Keys are scoped per merchant (`UNIQUE(merchant_id, idem_key)`).

- **Same key + same parameters** → the stored original response is returned with
  `Idempotent-Replay: true`; no second payment/refund/entry is created.
- **Same key + different parameters** → `409 idempotency_conflict`.
- A SHA-256 fingerprint of the normalized request body is stored with the
  response, and a concurrent duplicate loses the unique-key race and is resolved
  by re-reading the winner's result.

Concurrency guarantees come from `SELECT ... FOR UPDATE` row locks on the
payment, a unique ledger posting index, and check constraints
(`refunded_amount <= captured_amount`). 20 simultaneous refunds of a $100
payment can never produce more than $100 of refunds (see
`TestConcurrentRefundsNoOverRefund`).

---

## 6. Settlement

- A background worker sweeps captures older than `SETTLE_HORIZON` (default 24h),
  grouping by **merchant and UTC business day** of `captured_at`.
- One `settlement_batches` row exists per `(merchant, batch_date)` — a unique
  constraint plus a `pg_advisory_xact_lock` makes settlement idempotent.
- The batch stores `gross_captured`, `total_fees`, `total_refunds`,
  `refunded_fees`, and `net_amount = gross − refunds − fees + refunded_fees`.
- Re-running the sweep after a crash **skips completed days** and **takes over a
  stuck `processing` row**; payments are never put in two batches.
- Refunds before settlement shrink that day's batch; refunds after settlement
  create negative payable that nets into a later batch (the original historical
  batch is never edited).

Trigger manually:

```bash
curl -XPOST localhost:8080/v1/jobs/settle -H "Authorization: Bearer $OP" \
  -d '{"day":"2026-09-19"}'
```

---

## 7. Reconciliation

Per merchant per UTC day, the engine compares the internal ledger view against
the independent simulated acquirer feed (`channel_events`):

- checks: `capture_gross`, `capture_fees`, `refund_gross`, `refund_fees`,
  and `ledger_balance` (no posting group may be unbalanced);
- **|difference| > 5 cents** records a `discrepancy` item (≤ 5 cents is a match);
- one `reconciliation_runs` row per `(merchant, run_date)` means a re-run or a
  restarted process never produces duplicate records — the completed run is
  reused, and a crashed `running` row is retaken;
- discrepancies are surfaced in the API and logged by the worker
  (`RECON DISCREPANCY …`).

```bash
curl -XPOST localhost:8080/v1/jobs/reconcile/2026-09-19 -H "Authorization: Bearer $OP"
curl -s   localhost:8080/v1/reconciliations/2026-09-19 -H "Authorization: Bearer $OP"
```

---

## 8. Roles, authentication, masking, and audit

| Role | Powers |
|---|---|
| **admin** | Create/suspend merchants, rotate API keys, create users, read audit log |
| **operator** | All money movement and reads, **scoped to its own merchant only** |
| **auditor** | Read-only across merchants: payments, settlements, reconciliation, audit log |

Authentication:

- humans log in `POST /v1/auth/login` and receive a 12h HS256 JWT
  (`Authorization: Bearer <jwt>`);
- machines may call money endpoints directly with a merchant API key
  (`Authorization: Bearer cs_live_…` or `X-Api-Key`);
- operator keys are random 32-byte tokens; only their SHA-256 hash and a short
  non-reversible prefix are stored. The plaintext is returned **once** at
  creation/rotation.

Masking: user emails render as `a***@example.com`; API keys render as their
stored prefix (`cs_live_ab12…`); passwords are bcrypt-hashed and never returned.

Every privileged action (merchant/user provisioning, authorize, capture, void,
refund, settlement, reconciliation, key rotation, login) writes an append-only
`audit_logs` row **inside the same transaction** as the action, with actor,
merchant, action, target, optional masked detail, and source IP.

---

## 9. HTTP API

### Auth

| Method | Path | Role |
|---|---|---|
| POST | `/v1/auth/login` | public |

### Money movement (operator)

| Method | Path |
|---|---|
| POST | `/v1/payments/authorize` |
| POST | `/v1/payments/{id}/capture` |
| POST | `/v1/payments/{id}/void` |
| POST | `/v1/payments/{id}/refund` |
| GET  | `/v1/payments/{id}` |

### Read views (operator own / auditor all)

| Method | Path |
|---|---|
| GET | `/v1/payments`, `/v1/payments/{id}/refunds` |
| GET | `/v1/accounts/balance` |
| GET | `/v1/settlements`, `/v1/settlements/{id}` |
| GET | `/v1/reconciliations`, `/v1/reconciliations/{date}` |
| GET | `/v1/audit-logs` (admin/auditor) |

### Jobs (operator triggers its own merchant)

`POST /v1/jobs/settle`, `POST /v1/jobs/reconcile/{date}`

### Admin (admin role)

`POST /v1/admin/merchants`, `GET /v1/admin/merchants`,
`POST /v1/admin/merchants/{id}/rotate-key`,
`POST /v1/admin/merchants/{id}/status`,
`POST /v1/admin/users`, `GET /v1/admin/users`

A complete worked walk-through is in [`docs/example-session.md`](docs/example-session.md);
a runnable script is in [`scripts/demo.sh`](scripts/demo.sh).

---

## 10. Tests

```bash
# Pure unit tests (no DB): rounding boundaries, fee closure
go test ./internal/money/ -short -v

# End-to-end PostgreSQL tests (start the DB first)
docker compose up -d postgres
go test ./tests/integration/ -v
```

Coverage includes:

- concurrent refunds with no over-refund and fee closure;
- same-key replay vs. different-key conflict;
- illegal state transitions; expired 24h authorization; 90-day refund window;
- half-up rounding boundaries and proportional refund-fee closure;
- settlement idempotency, simulated crash recovery, and concurrent same-day runs;
- reconciliation 5-cent threshold, tamper detection, and run reuse.

---

## 11. Project layout

```
cmd/api, cmd/worker        HTTP server and standalone background worker
db/migrations              Numbered, immutable SQL migrations (also embedded)
db/queries                 sqlc queries
internal/money             integer-cent math and fee rules
internal/ledger            append-only double-entry posting rules
internal/payments          state machine, idempotency, postings
internal/settle            per-merchant/day settlement worker logic
internal/recon             reconciliation and discrepancy detection
internal/admin             merchants, users, API keys, login
internal/api               Chi router, middleware, handlers
internal/audit             audit-log writer
internal/worker            settlement + reconciliation loop
tests/integration          PostgreSQL-backed end-to-end tests
```
