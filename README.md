# Meridian TravelOps — Contract Settlement Backend

PHP 8.2 · Slim 4 · Eloquent (illuminate/database) · MySQL 8.

Backend for supplier contract **generation/signing** and cost **settlement**.
It deliberately excludes booking, payment processing and external notifications.
Records of signatures are operational audit data — the system does **not** claim
legal certification or qualified electronic signature status.

## Quick start (Docker)

```bash
docker compose up --build
```

This starts MySQL 8 and the PHP API, runs migrations and seeds sample data.

- API base: <http://localhost:8080>
- Health: `GET http://localhost:8080/health`
- MySQL is published on `localhost:3306` (user `root`, password `travelops`,
  db `travelops`) — override via `.env` / shell variables (`DB_PASSWORD`,
  `DB_NAME`, `APP_PORT`, `DB_PORT`).

Useful one-off commands:

```bash
docker compose exec app php bin/migrate.php     # apply schema
docker compose exec app php bin/seed.php        # insert sample data
docker compose exec app php bin/worker.php      # concurrency worker (tests)
docker compose exec app vendor/bin/phpunit      # run the test suite
```

## Domain model

| Table | Meaning |
|---|---|
| `contract_templates` | Text templates with `{{variable}}` placeholders |
| `contracts` | A contract; `current_version_id` points at the live version |
| `contract_versions` | Frozen content, sha256 hash, summary, ordered signers, 72h deadline |
| `signatures` | One row per signer per version, bound to a concrete `content_hash` |
| `allocation_rule_versions` | Versioned weighted cost-allocation rules |
| `import_batches` / `settlement_entries` | Imported supplier details (fixed-point `DECIMAL(18,4)`) |
| `daily_closes` | Immutable day marker with a row/total snapshot |
| `settlements` / `settlement_allocations` / `settlement_entry_links` | Settled totals, allocation lines and settled-entry links |

### Contracts & versions

- Initiating signing **renders the template**, freezes the complete content, a
  summary and `content_hash = sha256(content)` into a new pending version.
- Signing is **ordered**. Each confirmation names the exact hash; it is stored
  against that version. A confirmation is never portable: any content change
  requires initiating a **new version**, and prior signatures stay on the old one.
- The first signer can **withdraw** before any signature exists; after 72 hours
  a still-pending version cannot be signed and can be marked expired.
- Sign / withdraw / expiry serialize on the contract row (plus a version-level
  advisory lock) inside a DB transaction, and `UNIQUE(version, signer)`
  guarantees retries never double-confirm.

### Import

- One batch = one transaction: **any** validation failure rolls the whole batch
  back (no partial imports).
- `external_txn_id` is globally unique. Same id + same content = idempotent
  duplicate (skipped); same id + changed content = `409 external_id_conflict`.
- Amounts must be decimal strings with ≤ 4 fractional digits; JSON numbers/floats
  are rejected. Entries are grouped by **contract + currency + business date**.

### Cost allocation

- Rules are integer weights, versioned. A settlement permanently records which
  rule version and contract version (plus content hash) it used.
- Allocation uses the **largest-remainder (Hare)** method in integer 1/10000
  units: the lines always sum **exactly** to the original amount. Every leftover
  unit is attributed deterministically — a line may be marked `remainder_owner`,
  otherwise the largest fractional remainder wins, ties resolved by order.

### Daily close & corrections

- `POST /api/closes` snapshots the day and blocks later imports/settlements to it.
- Import and close take the same MySQL advisory lock per business date
  (`bizdate:<date>`) inside their transactions, plus `UNIQUE(business_date)`,
  so an import is either fully counted by the close or cleanly rejected — never
  missed, never a half-close.
- Closed records are immutable. Corrections are posted on a later open day as a
  **reversal** (`-amount`) and optional signed **adjustment**, linked to the
  original entry. Each original can be reversed once.

## HTTP API

All routes return JSON. Send the operator with `X-Actor: <name>` (stored on
every mutation as `*_by`, along with timestamps).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness incl. database |
| POST | `/api/templates` | Create a template `{code,name,body_template}` |
| GET | `/api/templates` | List templates |
| POST | `/api/contracts` | Create `{code,customer_name}` |
| GET | `/api/contracts` / `/{id}` | List / fetch contracts (fetch embeds versions) |
| POST | `/api/contracts/{id}/versions` | Render & initiate a version `{template_code?,variables,signers}` |
| GET | `/api/contracts/{id}/versions` | List versions |
| POST | `/api/versions/{id}/sign` | Confirm `{signer,content_hash}` |
| POST | `/api/versions/{id}/withdraw` | Withdraw before first signature |
| POST | `/api/versions/{id}/expire` | Mark expired if past 72h |
| POST | `/api/maintenance/expire-due` | Sweep all due versions |
| POST | `/api/contracts/{id}/rule-versions` | New allocation rules `{rules:[{target,weight,remainder_owner?}]}` |
| GET | `/api/contracts/{id}/rule-versions` | Active rule version |
| POST | `/api/imports` | Batch import `{batch_ref?,entries:[...]}` |
| GET | `/api/aggregations?contract_id=` | Totals by contract/currency/date |
| GET | `/api/entries` | List entries (filters: contract_id, currency, business_date) |
| POST | `/api/settlements` | Settle `{contract_id,currency,business_date,idempotency_key?}` |
| GET | `/api/settlements` | List settlements with allocations |
| POST | `/api/entries/{id}/reverse` | `{posting_date,adjustment?}` correction |
| POST | `/api/closes` | Close a day `{business_date}` |
| GET | `/api/closes` | List closes |

Error shape:

```json
{ "error": { "code": "external_id_conflict", "message": "...", "details": {} } }
```

### Walk-through with sample data

The seed creates contract **CT-2026-0001** (fully signed, active, 70/30 rules)
with EUR entries on 2026-09-18/19, and **CT-2026-0002** (pending, 3 ordered
signers).

```bash
# 1. aggregate the imported supplier details
curl -s localhost:8080/api/aggregations | jq .

# 2. settle the 18th for contract 1 (binds signed version + allocates 70/30)
curl -s -XPOST localhost:8080/api/settlements \
  -H 'Content-Type: application/json' -H 'X-Actor: finance' \
  -d '{"contract_id":1,"currency":"EUR","business_date":"2026-09-18"}' | jq .

# 3. close the day (snapshot; further imports to it are rejected)
curl -s -XPOST localhost:8080/api/closes \
  -H 'Content-Type: application/json' -d '{"business_date":"2026-09-18"}' | jq .

# 4. sign the pending contract in order
curl -s -XPOST localhost:8080/api/versions/2/sign -H 'Content-Type: application/json' \
  -d '{"signer":"procurement","content_hash":"<hash from GET /api/contracts/2>"}'
```

## Tests

```bash
docker compose exec app vendor/bin/phpunit
```

Coverage:

- **Signing races** — parallel confirmations of the same slot; sign-vs-withdraw.
- **Version binding** — settlements reject unsigned versions and pin the hash.
- **Duplicate import** — idempotent skip vs content conflict; full-batch rollback.
- **Allocation tail** — largest-remainder conservation across tricky amounts,
  explicit remainder ownership.
- **Close boundary** — parallel import vs close conservation, closed-day
  rejection, reversal/adjustment flow.

Concurrency tests fork `bin/worker.php` so contenders run as real OS processes
against the same MySQL database.

## Layout

```
docker/             Dockerfile + entrypoint
database/schema.sql MySQL schema
src/                Slim bootstrap, routes, models, services (domain logic)
bin/                wait-for-db, migrate, seed, concurrency worker
tests/              PHPUnit suite
```
