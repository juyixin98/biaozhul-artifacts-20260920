# CostLens — offline cloud-cost analysis backend

Import local billing CSVs into PostgreSQL, get exact daily/monthly rollups per
account and cost center, budget threshold alerts, and mean+2σ anomaly
detection. Read-only on local files: no cloud-account connectors, no
optimization recommendations, no frontend.

Stack: **Go 1.22 · Chi · sqlc · PostgreSQL 16 · pgx/v5**, started with Docker.

## Quick start

```bash
docker compose up -d --build          # Postgres + API on :8080
docker compose run --rm seed          # demo orgs, accounts, users, API keys
```

Seed prints four API keys:

| Role    | Key                      | Scope |
|---------|--------------------------|-------|
| admin   | `cl-sk-admin-0001`       | all orgs, rebuild |
| analyst | `cl-sk-analyst-0001`     | org-alpha: import + read |
| viewer  | `cl-sk-viewer-0001`      | org-alpha: read only |
| analyst | `cl-sk-analyst-beta-0001`| org-beta only |

```bash
# Import a bill batch (multipart or raw CSV body)
curl -s -X POST http://localhost:8080/v1/organizations/11111111-1111-1111-1111-111111111111/imports \
  -H "Authorization: Bearer cl-sk-analyst-0001" \
  -F "file=@examples/bills.csv"

# Monthly cost-center rollup (USD and EUR stay separate)
curl -s "http://localhost:8080/v1/summaries/monthly-cost-center" \
  -H "Authorization: Bearer cl-sk-analyst-0001"

# Set a monthly budget and watch threshold alerts
curl -s -X PUT http://localhost:8080/v1/budgets \
  -H "Authorization: Bearer cl-sk-analyst-0001" -H "Content-Type: application/json" \
  -d @examples/budget.json

# Full rebuild (admin) — must equal the incremental results
curl -s -X POST http://localhost:8080/v1/admin/rebuild \
  -H "Authorization: Bearer cl-sk-admin-0001"
```

## Bill CSV

Header (fixed order), at most 5000 data rows per batch:

```
account_code,resource_code,service,cost_date,currency,amount
```

- Dedup key: **account + resource + date**. Identical re-imports are idempotent.
- Same key, different amount/currency/service → conflict; **the whole batch rolls back**, response includes the CSV line number.
- Amounts are exact fixed-point decimals (`NUMERIC(20,6)`): ≤6 decimal places, no scientific notation, non-negative.
- Different currencies are never aggregated together.

More examples: `examples/bills_bad.csv` (conflict),
`examples/bills_too_many_rows.csv` (5001 rows).

## Rules

- **Summaries**: daily & monthly totals per account and per cost center, grouped by currency. Late bills recompute only affected days/months; `POST /v1/admin/rebuild` gives the same results from raw data.
- **Budgets**: per cost center + currency + month, immutable versions. One alert each at 50/75/90/100% first crossing; never duplicated per version; revisions keep the old alert history.
- **Anomalies**: daily total vs mean + 2·stddev of the previous 30 complete days (zero-spend days included, current day excluded). New accounts with a partial window → `insufficient_history`; zero variance → alert only on strictly greater spend; late data appends new evaluation versions and retains old ones.
- **RBAC**: analysts import only into authorized organizations; viewers are read-only; details, summaries and export all enforce org scope in SQL.

## Layout

```
migrations/                 SQL schema (also embedded into the API binary)
internal/db/queries/        sqlc queries        internal/db/dbgen/  generated
internal/money/             exact decimal parsing/statistics
internal/stats/             anomaly baseline rule
internal/csvio/             CSV parser (line-numbered errors, 5000 cap)
internal/service/           import pipeline, incremental rebuild, budgets, anomalies
internal/apiserver/         Chi HTTP layer
cmd/api  cmd/seed           binaries
test/                       PostgreSQL-backed integration tests
docs/api.md docs/design.md  API reference and design notes
```

## Development

```bash
make sqlc            # regenerate after editing queries/schema
make test            # unit + integration tests
make test-integration COSTLENS_TEST_DATABASE_URL=postgres://…/postgres?sslmode=disable
```

Configuration via environment:

| Variable | Default |
|----------|---------|
| `COSTLENS_DATABASE_URL` | `postgres://costlens:costlens@localhost:5432/costlens?sslmode=disable` |
| `COSTLENS_HTTP_ADDR` | `:8080` |
| `COSTLENS_TZ` | `UTC` |

Migrations run automatically at API startup inside a transaction.
