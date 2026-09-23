# CostLens HTTP API

Base URL: `http://localhost:8080`
All endpoints except `/healthz` require a header:

```
Authorization: Bearer <api-key>
```

Roles:

- `admin` — read access to every organization; may import, set budgets, trigger rebuild.
- `analyst` — may import bills and set budgets **only for organizations in their scope**; read access there.
- `viewer` — read-only access to organizations in their scope. Every import/budget/rebuild returns `403`.

All money is returned as exact decimal strings (e.g. `"225.500000"`), never JSON numbers.
Every read (details, summaries, export) is filtered server-side by the caller's organization scope; there is no unscoped path.

## Error format

```json
{ "error": "content_conflict", "message": "…", "line": 3 }
```

`line` is the 1-based CSV line (header = 1, first data row = 2) for import failures.

| HTTP | error codes |
|------|--------------|
| 400 | `bad_upload`, `invalid_filter`, `invalid_json` |
| 401 | `unauthorized` (missing/invalid key) |
| 403 | `forbidden`, `forbidden_org` |
| 404 | `cost_center_not_found`, `not_found` |
| 409 | `content_conflict` |
| 422 | `csv_parse`, `unknown_account`, `duplicate_key_in_file`, `invalid_budget` |

## Catalog

### `GET /v1/organizations`
Organizations visible to the caller.

### `GET /v1/cost-centers?org_id=<uuid>[&org_id=…]`
### `GET /v1/accounts?org_id=<uuid>`

## Bills

### `POST /v1/organizations/{org_id}/imports`

Send the CSV either as `multipart/form-data` with a `file` field, or the raw body with
`Content-Type: text/csv` (`?filename=` optional).

CSV header (fixed column order), at most **5000** data rows:

```
account_code,resource_code,service,cost_date,currency,amount
```

- `cost_date`: `YYYY-MM-DD`; `currency`: 3 uppercase letters; `amount`: exact decimal, ≤ 6 fraction digits, non-negative, no scientific notation.
- Dedup key: `(account, resource, cost_date)`. An identical existing row is an idempotent duplicate (counted, not inserted). A same-key row with different `amount`/`currency`/`service` is `content_conflict`; **the whole batch rolls back**.
- New `resource_code` values are auto-registered against their service.

`201`:
```json
{
  "import_id": "…", "raw_row_count": 5, "inserted_row_count": 3,
  "duplicate_row_count": 2, "min_date": "2026-09-01T00:00:00Z",
  "max_date": "2026-09-30T00:00:00Z", "accounts": ["…"]
}
```

### `GET /v1/imports?org_id=…&limit=&offset=`
Import audit log, including failed batches (`status`, `error_line`, `error_code`, `error_message`).

## Cost details & export

### `GET /v1/costs?org_id=…`
Filters: `account_id`, `currency`, `date`, `from`, `to`, `limit` (default 1000, max 5000), `offset`.

### `GET /v1/export/costs.csv?org_id=…`
Same filters; streams CSV (`account_code,resource_code,service,cost_date,currency,amount`)
from one read transaction, so concurrent imports/rebuilds cannot interleave a partial view.

## Summaries

Currencies are grouped, never summed together. Filters: `account_id` / `cost_center_id`,
`currency`, `from`, `to`; monthly views accept `period=YYYY-MM-01`.

- `GET /v1/summaries/daily-account`
- `GET /v1/summaries/monthly-account`
- `GET /v1/summaries/daily-cost-center`
- `GET /v1/summaries/monthly-cost-center`

## Budgets

### `PUT /v1/budgets`
```json
{ "cost_center_id": "…", "currency": "USD", "period": "2026-09-15", "amount": "500.00" }
```
`period` is normalized to the first of the month. Each call appends a new immutable
**budget version** (`version` increments). Alerts always reference the version that
justified them, so a revision never rewrites history.

When the month-to-date cost-center total first reaches **50% / 75% / 90% / 100%**
of a budget version, one alert per threshold is created. Re-crossing (re-imports,
rebuilds) never duplicates an alert for the same `(budget version, threshold)`.

### `GET /v1/budgets?org_id=…`
Current (highest) version per `(cost center, currency, period)`.

### `GET /v1/budget-alerts?org_id=…`
Filters: `cost_center_id`, `currency`, `period`.

## Anomaly detection

Rule per `(account, currency, day)`: compare the day's total against
**mean + 2·population-stddev** of the 30 immediately preceding complete days.
Zero-spend days are included as zero; the current day never enters its own baseline.

- Account did not exist for the full 30-day window → `insufficient_history` (never an alert).
- Zero variance → threshold equals mean; anomaly only when actual is **strictly greater** (equal is normal).
- Evaluations are append-only and versioned by run. Late data that shifts a baseline inserts new evaluations; prior alerts/evaluations stay.

### `GET /v1/anomalies?org_id=…`
Latest evaluation per `(account, currency, day)` where the result is an anomaly.
Filters: `account_id`, `currency`, `from`, `to`.

### `GET /v1/anomalies/history?org_id=…`
All evaluation versions (anomaly / normal / insufficient_history), newest first.

## Admin

### `POST /v1/admin/rebuild`  (admin only)
Truncates and recomputes every summary from raw `costs`, then re-evaluates budgets and
anomalies. Imports and rebuilds share one transaction-level advisory lock, so a rebuild
can never lose or double-count a concurrent import. Returns:
```json
{ "status": "ok", "accounts_recomputed": 3 }
```

## `GET /healthz`
Unauthenticated liveness probe.
