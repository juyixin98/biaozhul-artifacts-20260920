# Local domain lifecycle engine

A fully **local** simulation of a registrar-style domain name lifecycle engine:
registration, renewal, expiry, 30-day redemption grace, pending-delete release,
and inter-reseller transfers — backed by reseller **credit ledger** billing in
integer cents. **No real registrar, DNS or payment provider is ever contacted.**

* Go 1.22 · Echo · sqlx · PostgreSQL 16 · Docker Compose
* Injectable clock: the simulated 5-day transfer wait and 30-day redemption
  period need zero real waiting in tests and demos
* SERIALIZABLE transactions + append-only ledger: concurrent registration,
  renewal, transfer and expiry processing can't double-own or double-charge

## Contents

| Path | Purpose |
| --- | --- |
| `cmd/server` | HTTP API + background worker (one binary) |
| `cmd/simtimedemo` | End-to-end walkthrough driven by a manual clock |
| `internal/store/migrations` | Embedded SQL migration (`0001_init.sql`) |
| `internal/domains` | Registration, renewal, restore, expiry sweep |
| `internal/transfers` | Auth-coded transfer state machine + freeze/capture |
| `internal/ledger` | Integer-cent reseller credits, holds, idempotency |
| `internal/prices` | Admin-managed, versioned TLD price rules |
| `internal/accounts` | Resellers, customers, bearer-token auth |
| `internal/worker` | Restart-safe, idempotent, single-winner background jobs |
| `internal/clock`, `internal/names`, `internal/codec`, `internal/authcode` | Support packages |
| `internal/engine/*_test.go` | Concurrency, boundary, timeout, price, restore tests |

## Domain lifecycle and legal transitions

```
                         register (charge register_fee * years)
   available  ───────────────────────────────────────►  registered
       ▲                                                     │
       │                                                     │ renew (renew_fee * years; extends from expiry)
       │                                                     │
       │ expire at expires_at                                ▼
       │                                                 expired
       │                                                     │  +30 days (REDEEM_PERIOD)
       │                                                     ▼
       │                                                 redeemable ── restore (restore_fee + 1 year)
       │                                                     │  +5 days (PENDING_DELETE)
       │                                                     ▼
       │                                                 pending_delete  (no renew, no restore)
       │                                                     │  +5 days
       └─────────────────────────────────  purge (row deleted, name released)
```

* `registered → expired` at `expires_at` (+ `EXPIRED_GRACE`, default 0).
* `expired → redeemable` after the **30-day** redemption period.
* `redeemable → pending_delete` after 5 days; `pending_delete → released` after
  another 5 days, when the row is deleted and the name can be registered again.
* **Renewal** is legal only while `registered`; it extends from the stored
  expiry date (never from "now"), 1–10 years per request.
* **Restore** is legal from `expired` or `redeemable` for `restore_fee` plus
  one renewal year, and grants a fresh year from the restore time.
* Transitions are computed from explicit deadline columns; the worker only
  applies transitions whose deadline has passed.

### Transfer state machine and billing timing

```
gaining reseller requests with domain + 16-char auth code
   │  transfer_fee is FROZEN immediately: available → held   (billing point #1)
   │  domain: registered → transferring
   ▼
pending_approval ── losing reseller approves ──► seller_approved
   │                                                  │ after 5-day simulated wait
   │ reject (losing)                                  ▼
   │ cancel (gaining)                            completed
   │ 5-day approval window elapses              fee CAPTURED: held → consumed (point #2)
   ▼                                            ownership moves, +1 year, domain → registered
failed/rejected/canceled: FROZEN fee RELEASED (held → available); domain → registered
```

* The fee is snapshotted on the transfer row at request time; **a later price
  adjustment never changes an accepted transfer**.
* Freeze, capture and release are ledger postings written in the same
  transaction as the transfer/domain state change, so failure never moves
  money without state (or state without money).

## Uniqueness and name rules

Uniqueness is enforced on the **canonical** name (`domains.canonical_name`,
unique index; the row is removed only at purge):

1. Whitespace trimmed; a **single trailing root dot** (`example.com.`) is
   stripped. Two trailing dots are invalid.
2. ASCII letters are **case-folded** (`Example.COM` ≡ `example.com`).
3. Internationalized names are **NFC-normalized and converted to Punycode**
   with IDNA2008 registration rules (`bücher.example` →
   `xn--bcher-kva.example`); the A-label is what is stored, compared and billed.
4. Labels are 1–63 chars, total ≤ 253, at least two labels, no empty labels.

## Concurrency and billing guarantees

* Every mutating operation is one PostgreSQL **SERIALIZABLE** transaction
  (automatic retry on `40001`/`40P01`) that locks the affected rows
  `FOR UPDATE`.
* Two racing registrations of one canonical name: exactly one succeeds and is
  charged once; the other receives `domain_taken`.
* Renewal racing the expiry sweep at the boundary has one deterministic
  outcome per iteration — renewed (and charged) **or** expired (renewal
  rejected, never charged).
* Money is integer cents across `resellers(balance_cents, held_cents)`,
  append-only `billing_transactions` and `ledger_entries`. Check constraints
  forbid negative balances.
* Requests may carry `idempotency_key` (per reseller + transaction kind).
  Retries (including concurrent ones, serialized via a transaction-scoped
  advisory lock) return the original transaction and never charge twice.
* Worker jobs are idempotent and restart-safe (each row transition commits
  independently); across replicas a session advisory lock elects one worker
  per tick, so repeated execution can't double-process.

## Roles and secrets

* **customer** — sees only its own domains; may renew/restore them.
* **reseller** — manages its own customers, registers/renews domains for them,
  spends credit, and initiates/approves transfers only where it is a party.
* **admin** — creates resellers, tops up credit, manages TLD prices, views the
  event log.

Auth tokens are random 256-bit values stored only as SHA-256 digests
(plaintext shown once at creation). Transfer auth codes are 16 characters from
an unambiguous alphabet, stored **AES-256-GCM encrypted** (`auth_cipher`,
never serialized to JSON) and compared in constant time. Request logs record
only method/path/status/latency — never headers, bodies or query strings.

## Quick start (Docker Compose)

```bash
docker compose up --build
```

On first boot the server migrates the schema and prints a one-time admin
token:

```
engine: bootstrap admin token (shown once): dlt_xxxx…
```

Create a reseller with credit, then a customer:

```bash
ADMIN=dlt_xxxx
curl -s -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"name":"acme","starting_cents":500000}' \
  http://localhost:8080/v1/admin/resellers
# -> {"reseller":{...},"token":"dlt_reseller..."}

RTOK=dlt_reseller...
curl -s -H "Authorization: Bearer $RTOK" -H 'Content-Type: application/json' \
  -d '{"name":"bob"}' http://localhost:8080/v1/reseller/customers
```

Register (note the trailing dot and mixed case — canonicalized):

```bash
curl -s -H "Authorization: Bearer $RTOK" -H 'Content-Type: application/json' \
  -d '{"name":"Shop.Example.COM.","customer_id":1,"years":1}' \
  http://localhost:8080/v1/reseller/domains
# -> {"domain":{"canonical_name":"shop.example.com",...},"auth_code":"ABCD…","charge":{...}}
```

Transfer between two resellers:

```bash
# gaining reseller (must first create its own target customer)
curl -s -H "Authorization: Bearer $GAINER" -H 'Content-Type: application/json' \
  -d '{"domain_name":"shop.example.com","auth_code":"ABCD…","customer_id":2}' \
  http://localhost:8080/v1/reseller/transfers
# losing reseller approves
curl -s -X POST -H "Authorization: Bearer $LOSER" \
  http://localhost:8080/v1/reseller/transfers/1/approve
# ~5 simulated days later the worker completes it automatically; or inspect:
curl -s -H "Authorization: Bearer $LOSER" \
  http://localhost:8080/v1/reseller/transfers/1
```

## Simulated-time example (no real waiting)

`simtimedemo` drives a manual clock through registration, renewal, transfer
approval/wait/completion, expiry, restore and full release/re-registration,
printing statuses and balances at each step. Run it against the Compose
database (the DB container is not published on the host by default):

```bash
docker compose up -d --build
docker compose run --rm --entrypoint /app/simtimedemo api
```

Or against a reachable DSN:

```bash
go run ./cmd/simtimedemo \
  -db 'postgres://domain:domain@localhost:5433/domain?sslmode=disable'
# (publish the DB on a free host port first, e.g. uncomment ports: ["5433:5432"]
#  in docker-compose.yml)
```

It truncates application tables first — use it only on a scratch database.

## Configuration

All settings have defaults; override via environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HTTP_ADDR` | `:8080` | Listen address |
| `DATABASE_URL` | `postgres://domain:domain@localhost:5432/domain?sslmode=disable` | DSN |
| `CODEC_KEY_HEX` | dev key | 64 hex chars (AES-256); set your own outside dev |
| `SWEEP_INTERVAL` | `10s` | Domain expiry sweep interval |
| `POLL_INTERVAL` | `5s` | Transfer timeout/completion poll |
| `APPROVAL_WINDOW` | `120h` | Seller approval window |
| `EXPIRED_GRACE` | `0s` | Extra grace after `expires_at` |
| `REDEEM_PERIOD` | `720h` | Redemption period (30 days) |
| `PENDING_DELETE` | `120h` | Pending-delete phase (5 days) |

The post-approval simulated registry wait is fixed at 5 days
(`cmd/server` constructs it explicitly); tests inject arbitrary values.

## Migrations

SQL migrations live in `internal/store/migrations` and are **embedded** into
the binary. On startup the server applies any not yet recorded in
`schema_migrations`; applying them repeatedly is safe.

## Tests

```bash
# unit tests (no database)
go test ./internal/names/...

# integration tests need a PostgreSQL database. Default DSN (peer auth):
#   host=/var/run/postgresql user=admin dbname=domainengine_test sslmode=disable
createdb domainengine_test           # once
go test ./... -race
# or point elsewhere:
DATABASE_URL='postgres://domain:domain@localhost:5432/domain?sslmode=disable' \
  go test ./... -race
```

The suite (`internal/engine`) covers:

* **concurrent rush registration** of canonical variants — one owner, one charge
* **idempotency** under concurrent retries for register/renew/top-up
* **renewal boundary** and **renew-vs-sweep race** with deterministic outcomes
* full expiry ladder, **restore** from expired/redeemable, and re-registration
* transfer **happy path** with the 5-day simulated wait, **reject/cancel
  release**, **approval-timeout failure**, wrong auth code, insufficient funds
* **price change mid-transfer** (snapshot preserved) and new prices applying to
  new business
* RBAC over HTTP, auth-cipher non-disclosure, append-only ledger consistency

## HTTP surface (summary)

```
GET  /health

POST /v1/admin/resellers               GET  /v1/admin/resellers
POST /v1/admin/resellers/:id/topup
POST /v1/admin/prices                  GET  /v1/admin/prices
GET  /v1/admin/events

POST /v1/reseller/customers            GET  /v1/reseller/customers
POST /v1/reseller/domains              GET  /v1/reseller/domains
GET  /v1/reseller/domains/:name
POST /v1/reseller/domains/:name/renew
POST /v1/reseller/domains/:name/restore
POST /v1/reseller/domains/:name/authcode
POST /v1/reseller/transfers            GET  /v1/reseller/transfers
GET  /v1/reseller/transfers/:id
POST /v1/reseller/transfers/:id/approve
POST /v1/reseller/transfers/:id/reject
POST /v1/reseller/transfers/:id/cancel
GET  /v1/reseller/billing

GET  /v1/customer/domains              GET  /v1/customer/domains/:name
POST /v1/customer/domains/:name/renew
POST /v1/customer/domains/:name/restore
GET  /v1/customer/transfers            GET  /v1/customer/transfers/:id
```

All mutating endpoints accept an optional `"idempotency_key"` in the JSON body.
