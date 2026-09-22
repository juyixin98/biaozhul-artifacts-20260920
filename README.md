# CostLens

Offline cloud-cost analysis backend. It imports billing **CSV files from
local disk only** — there are no cloud-account connectors, no optimization
recommendations, and no UI. Built with Go, Chi, sqlc and PostgreSQL.

## Features

- **Organizations → cost centers → accounts**; users are `admin`, `analyst`
  (import only into granted orgs) or `viewer` (read-only). Records,
  summaries, anomalies and CSV exports share one org-scoped isolation layer.
- **Idempotent CSV import**, ≤5000 rows/batch, exact `NUMERIC(20,6)` money.
  Dedup on `(account, resource, date)`; same key with different content is a
  conflict that reports line numbers and rolls back the **whole batch**.
- **Daily & monthly summaries** at account and cost-center scope; currencies
  are never mixed. Late billing recomputes only the affected slices, and
  incremental results are identical to a full rebuild.
- **Versioned monthly budgets** per cost center + currency, with alerts on
  the first crossing of **50/75/90/100 %**, never repeated for the same
  budget version; old versions and alerts are retained.
- **Anomaly detection** against the previous 30 *complete* days
  (zero-spend days included, current day excluded):
  `mean + 2 × population σ`. Explicit outcomes for insufficient history and
  zero variance; late data appends new evaluation versions without deleting
  old ones.
- A **full rebuild** runs in one transaction under the same advisory lock as
  imports, so an import during a rebuild blocks and then lands cleanly —
  nothing is lost or double-counted.

## Quick start (Docker)

```bash
docker compose up --build
```

This starts PostgreSQL, runs migrations, seeds demo data (two organizations,
three accounts, users, budgets, 60–90 days of billing including an anomaly),
and serves the API on http://localhost:8080. Seed user tokens are printed by
the `api` container on first boot:

```bash
docker compose logs api | grep "seed user"
```

Then:

```bash
TOKEN=clt_...                 # admin token from the logs
curl -s http://localhost:8080/healthz
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8080/api/v1/accounts
```

Set `COSTLENS_SEED: "false"` in `docker-compose.yml` for a clean database.

## Local development

Requires Go 1.22, sqlc 1.26, Docker (for Postgres), and `golangci-lint`
(optional).

```bash
make db            # start a throwaway Postgres on localhost:55432
make sqlc          # regenerate internal/db from queries/
make migrate       # apply migrations (also automatic on server start)
make run           # run the API on :8080
make test          # unit + integration tests (needs the test DB)
```

The integration tests use

```
TEST_DATABASE_URL=postgres://costlens:costlens@localhost:55432/costlens?sslmode=disable
```

(the default from `make db`). Pure unit tests run without a database.

## Configuration

| env var | default | meaning |
|---|---|---|
| `DATABASE_URL` | `postgres://costlens:costlens@localhost:5432/costlens?sslmode=disable` | PostgreSQL DSN |
| `COSTLENS_HTTP_ADDR` | `:8080` | listen address |
| `COSTLENS_AUTOMIGRATE` | `true` | apply embedded migrations at startup |
| `COSTLENS_SEED` | `false` | seed demo catalog/users/billing |

## Layout

```
cmd/server          HTTP server + demo seeder
internal/api        Chi handlers and routing
internal/auth       bearer-token auth, role/org authorization
internal/csvparse   strict CSV validation (5000-row cap, line numbers)
internal/db         sqlc-generated queries (regenerate with make sqlc)
internal/decimalx   NUMERIC<->decimal helpers, exact sqrt
internal/migrate    embedded SQL migration runner
internal/service    import tx, summaries, budgets, anomalies, rebuild
queries/            sqlc queries            migrations -> internal/migrate/sql
testdata/           sample CSV
API.md              endpoint reference
```

## CSV format

```
resource_id,service,date,currency,amount
i-0f1a2b3c,compute,2026-08-01,USD,42.175000
```

See [`testdata/sample_billing.csv`](testdata/sample_billing.csv) and
[`API.md`](API.md) for full semantics.

## Consistency model

1. Every import is one transaction: stage rows in a TEMP table via COPY,
   detect intra-file and DB conflicts, insert new keys, then recompute.
2. All writers (imports, budget version changes, rebuilds) take
   `pg_advisory_xact_lock(88117335)`, so concurrent duplicate imports
   serialize and a rebuild can never race an import: the loser waits and
   applies against committed data.
3. Summary recomputation is an *upsert from `billing_records`* for the exact
   touched slices — the same statement form the rebuild uses over the whole
   table — which is why incremental and rebuilt state match.
4. Budget alerts are immutable (`UNIQUE(budget_id, threshold_pct)`); anomaly
   evaluations are append-only with per-key versions.
