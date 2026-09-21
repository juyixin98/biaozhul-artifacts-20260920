# ClearSettle

Local **simulated** payment, settlement and reconciliation engine — no real payment
network is ever contacted. Built with Go 1.22, [Chi](https://github.com/go-chi/chi),
[sqlc](https://sqlc.dev), PostgreSQL 16 and Docker Compose.

- [Money & fee rules](#money--fee-rules)
- [Payment lifecycle](#payment-lifecycle)
- [Double-entry ledger](#double-entry-ledger)
- [Idempotency & concurrency](#idempotency--concurrency)
- [Settlement & reconciliation](#settlement--reconciliation)
- [Roles, masking & audit](#roles-masking--audit)
- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [Local development](#local-development)
- [API reference](#api-reference)
- [Worked example](#worked-example-end-to-end-simulated-transaction)
- [Project layout](#project-layout)
- [Tests](#tests)

## Money & fee rules

- Every amount is an **integer number of cents** (`BIGINT`). Floating point is
  never used for money.
- Default processing fee is **2.9% + $0.30**, configurable per merchant
  (`fee_bps` = 290, `fee_fixed_cents` = 30).
- Fee on a captured amount `gross`:

  ```
  fee = round_half_up(gross * bps / 10000) + fixed
  ```

  Half-up (commercial) rounding is done with integer arithmetic only
  (`(x + 5000) / 10000`), so e.g. 2.9% of **500** cents = 14.5 → **15** cents.
  See `internal/money/money.go` and its boundary tests.
- **Refund fee handling.** The $0.30 fixed component is **non-refundable**
  (acquirer cost). Only the proportional 2.9% component is released, pro-rata:

  ```
  released(totalRefunded) = propFee(captured) − propFee(captured − totalRefunded)
  ```

  The released amount is recomputed from the *cumulative* refunded total, so the
  sum released is independent of the order/size of partial refunds and can never
  exceed the proportional fee. A full refund of 10000c releases exactly 290c,
  never 320c.

## Payment lifecycle

```
authorized ──capture──► captured ──settle──► settled
    │                       │  │                 │
    │            partial/full┘  └──refund────────► partially_refunded / refunded
    └──void (<=24h)──► voided (terminal)
```

- **Authorize** holds funds only; nothing is posted to the ledger until capture.
- **Capture** and **void** must happen within **24 hours** of the authorization
  (`auth_expires_at`). Partial capture is allowed (≤ authorized).
- **Refunds** are allowed from `captured`, `partially_refunded` and after
  settlement, for **90 days after settlement** (`refund_deadline`). Cumulative
  refunds cannot exceed the captured amount — enforced under a row lock.
- Any other transition is rejected with HTTP 422 (`illegal state transition`).

## Double-entry ledger

Each merchant has its own `gateway_cash` (asset), `payable` (liability),
`fee_revenue` (equity contra) and `suspense` accounts. Every posting is a
balanced set (signed amounts sum to zero):

| Event | gateway_cash | payable | fee_revenue |
|---|---:|---:|---:|
| capture `g`, fee `f` | **+g** | **−(g−f)** | **−f** |
| refund `a`, fee release `fr` | **−a** | **+(a−fr)** | **+fr** |
| settlement net `n` | **−n** | **+n** | — |

- `ledger_entries` is **append-only**: a trigger rejects `UPDATE`/`DELETE`.
- Errors are corrected by a **new reversing pair** through `suspense`
  (`POST /v1/admin/corrections`); history is never mutated.
- Settlement of a fully/over-refunded payment can post a *negative* net
  (clawback); the same balanced legs apply with reversed signs.

## Idempotency & concurrency

- Every write requires an `Idempotency-Key` header. Params are canonically hashed
  (JSON re-serialized with sorted keys).
- **Same key + same params → original result** is replayed (HTTP 200), and the
  action is **not** re-executed — no duplicate ledger posting.
- **Same key + different params → 409** (`idempotency key was already used with
  different parameters`).
- Payment rows are locked `SELECT … FOR UPDATE`; refunds/captures/voids
  therefore serialize per payment and can never over-refund or double-post.
  Refund contention uses READ COMMITTED + pessimistic locks (waiters re-check
  against the latest committed state), avoiding serializable 40001 hot-spot
  failures.
- State, balances and ledger entries commit in the **same transaction** as the
  idempotency record.

## Settlement & reconciliation

- `POST /v1/merchants/{id}/settle?date=YYYY-MM-DD` (admin) settles every
  eligible payment created on/before that date into one batch.
- **Crash-safe / re-runnable**: one batch per (merchant, date); each payment is
  committed in its own transaction guarded by `UNIQUE(settlement_items.payment_id)`
  and `FOR UPDATE SKIP LOCKED`. A killed process simply resumes — already-settled
  payments are skipped, and the batch finalizes only once.
- `POST …/reconcile?date=` (admin) first ensures settlement, then compares
  internal captures/refunds with the simulated gateway statement feed and checks
  the ledger identity `gateway_cash + payable + fee_revenue = 0` on the
  as-of-timestamp snapshot.
- Any difference **> 5 cents** creates a `discrepancies` row (missing
  statement/payment/refund, amount mismatch, ledger imbalance). Re-running a date
  returns the existing run and never duplicates discrepancies.
- The reconcile CLI (`cmd/reconcile`) iterates all active merchants and is the
  intended cron entry point.

## Roles, masking & audit

- **admin** — merchant/key administration, settlement, reconciliation,
  corrections (platform-level, no merchant id).
- **operator** — money moves for its **own** merchant only.
- **auditor** — read-only.
- API keys are stored only as SHA-256 hashes; the plaintext is shown **once** at
  issue time. Listings return a masked prefix (`sk_op_a1b2c3d4••••••••••••`).
- Only card last4 are stored, rendered as `•••• •••• •••• 4242`.
- Key actions write `audit_events` with actor, action, target and metadata.

## Quick start (Docker Compose)

```bash
docker compose up --build
```

This starts PostgreSQL and the API on **http://localhost:8080**, applies
migrations automatically, and seeds a demo merchant. The first log lines print
the secrets — save them:

```
bootstrapped platform admin key: sk_ad_demoadminkey000000000000000000
demo operator key (save it): sk_op_xxxxxxxx...
```

Health check:

```bash
curl -s localhost:8080/healthz
```

Run the nightly job any time (safe to repeat / interrupt):

```bash
docker compose exec api reconcile -admin-key sk_ad_demoadminkey000000000000000000
```

## Local development

Requirements: Go 1.22, Docker (or a local Postgres 16), `sqlc` v1.25+
(only needed when editing queries).

```bash
# 1. database
docker compose up -d postgres

# 2. regenerate sqlc code after editing db/queries (committed output is included)
sqlc generate

# 3. run
export CLEARSETTLE_DATABASE_URL='postgres://clearsettle:clearsettle@localhost:5433/clearsettle?sslmode=disable'
go run ./cmd/server

# background reconciler
go run ./cmd/reconcile -admin-key sk_ad_... -date 2026-01-15
```

Migrations live in `db/migrations/0001_init.sql` (identical copy embedded for
auto-migrate in `internal/store/migrations/`). They are tracked in
`schema_migrations`; each file applies in one transaction.

## API reference

All `/v1` routes need `Authorization: Bearer <key>` (or `X-Api-Key`). Write
routes additionally need `Idempotency-Key: <unique>`.

| Method & path | Role | Purpose |
|---|---|---|
| `POST /v1/admin/merchants` | admin | create merchant (returns operator key once) |
| `GET  /v1/admin/merchants` | admin | list merchants |
| `PATCH /v1/admin/merchants/{id}` | admin | update name/fee/active |
| `POST /v1/admin/keys` | admin | issue admin/operator/auditor key |
| `GET  /v1/admin/keys` | admin | list masked keys |
| `POST /v1/admin/statements/sync` | admin | regenerate simulated gateway feed |
| `POST /v1/admin/statements` | admin | insert/override one statement row |
| `POST /v1/admin/corrections` | admin | reversing ledger correction via suspense |
| `POST /v1/payments/authorize` | operator/admin | authorize |
| `POST /v1/payments/capture` | operator/admin | capture (full/partial) |
| `POST /v1/payments/void` | operator/admin | void within 24h |
| `POST /v1/payments/refund` | operator/admin | refund (full/partial) |
| `POST /v1/merchants/{id}/settle` | admin | run/resume daily settlement |
| `POST /v1/merchants/{id}/reconcile` | admin | run/resume reconciliation |
| `GET  /v1/payments/{id}` | any (scoped) | payment detail (masked card) |
| `GET  /v1/merchants/{id}/payments` | any (scoped) | list payments |
| `GET  /v1/merchants/{id}/refunds` | any (scoped) | list refunds |
| `GET  /v1/merchants/{id}/batches` | any (scoped) | settlement batches |
| `GET  /v1/batches/{id}` | any (scoped) | batch with items |
| `GET  /v1/merchants/{id}/reconcile` | any (scoped) | reconciliation runs |
| `GET  /v1/merchants/{id}/reconcile/{date}` | any (scoped) | run + discrepancies |
| `GET  /v1/merchants/{id}/balances` | any (scoped) | ledger account balances |
| `GET  /v1/ledger/{refType}/{refId}` | any (scoped) | immutable entries for a payment/refund/batch/manual |
| `GET  /v1/audit` | any (scoped; admin = all) | audit log |

Errors: `403` forbidden/scoping, `404` not found, `409` idempotency conflict,
`422` validation / illegal transition / window closed / over-refund.

## Worked example (end-to-end simulated transaction)

```bash
ADMIN=sk_ad_demoadminkey000000000000000000
# operator key is printed by the server at startup; export it:
OP=sk_op_....

# 1. Authorize $50.00
curl -s -X POST localhost:8080/v1/payments/authorize \
  -H "Authorization: Bearer $OP" -H 'Idempotency-Key: demo-auth-1' \
  -d '{"merchant_id":"<mid>","amount_cents":5000,"card_last4":"4242"}'
# -> id = PID, status authorized, card "•••• •••• •••• 4242"

# 2. Capture (fee = half_up(5000*290/10000)+30 = 145+30 = 175)
curl -s -X POST localhost:8080/v1/payments/capture \
  -H "Authorization: Bearer $OP" -H 'Idempotency-Key: demo-cap-1' \
  -d '{"payment_id":"<PID>"}'
# -> captured, fee_cents 175; ledger: gateway +5000, payable -4825, fee -175

# 3. Partial refund $20.00 (proportional release 58c; merchant net 1942)
curl -s -X POST localhost:8080/v1/payments/refund \
  -H "Authorization: Bearer $OP" -H 'Idempotency-Key: demo-ref-1' \
  -d '{"payment_id":"<PID>","amount_cents":2000,"reason":"returned item"}'

# 4. Repeating the refund with the SAME key returns the original refund (200),
#    posts nothing new; changing the amount under that key returns 409.

# 5. Settle the day
curl -s -X POST "localhost:8080/v1/merchants/<mid>/settle" \
  -H "Authorization: Bearer $ADMIN"
# -> total 3000, fee 117, net 2883

# 6. Build the simulated gateway feed and reconcile (clean -> 0 discrepancies)
curl -s -X POST localhost:8080/v1/admin/statements/sync -H "Authorization: Bearer $ADMIN" \
  -d '{"merchant_id":"<mid>"}'
curl -s -X POST "localhost:8080/v1/merchants/<mid>/reconcile" -H "Authorization: Bearer $ADMIN"

# 7. Introduce a 100c feed error -> one amount_mismatch discrepancy (>5c)
curl -s -X POST localhost:8080/v1/admin/statements -H "Authorization: Bearer $ADMIN" \
  -d '{"merchant_id":"<mid>","ref_id":"<PID>","kind":"capture","amount_cents":4900,"stmt_date":"2026-01-15"}'
curl -s -X POST "localhost:8080/v1/merchants/<mid>/reconcile" -H "Authorization: Bearer $ADMIN"
# (runs for a new date; the prior date stays immutable)
```

A copy-paste version is in `examples/demo.sh` (uses `jq`).

## Project layout

```
cmd/server         HTTP API + auto-migrate + demo seed
cmd/reconcile      nightly settle+reconcile worker
db/migrations      SQL schema (embedded copy in internal/store/migrations)
db/queries         sqlc queries
internal/db        generated sqlc code (committed)
internal/money     cents, half-up fee, proportional refund-fee rules
internal/domain    state machine, windows, clock
internal/service   transaction/ledger/settlement/reconciliation/idempotency core
internal/api       Chi router, auth middleware, masking, handlers
internal/store     pgx pool, transaction helpers, embedded migrations
internal/integration  Postgres-backed tests (boots an ephemeral Docker Postgres)
```

## Tests

```bash
go test ./...                      # unit tests; integration auto-starts a docker postgres
CLEARSETTLE_TEST_DATABASE_URL=postgres://… go test ./internal/integration/...
```

Coverage required by the brief:

- **concurrent** partial refunds (12-way race, total never exceeds captured),
  concurrent capture and void/capture races;
- **duplicate requests** via idempotency replay (payment and refund) and
  same-key-different-params conflict;
- **illegal transitions** (capture after void, void after capture, refund of
  authorized, over-capture, over-refund, expired 24h / 90d windows);
- **rounding boundaries** for half-up fees and order-independent refund fees;
- **settlement interruption/resume** (context cancelled mid-batch → resume
  produces exactly one batch/items/entries), plus reconciliation recovery from a
  stale `running` run, and the 5-cent discrepancy threshold.
