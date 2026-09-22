# CostLens API

Offline billing analysis backend. All endpoints except `/healthz` require:

```
Authorization: Bearer <api_token>
```

Roles:

- **admin** — full access across all organizations, manages catalog/users/budgets, triggers rebuilds.
- **analyst** — read access to granted organizations; may import CSV only into accounts belonging to granted organizations.
- **viewer** — read-only access to granted organizations.

All money fields serialize as **fixed-point decimal strings** (never floats)
with a fixed scale: spend/limit/budget amounts use exactly 6 fractional digits
(e.g. `"42.175000"`); anomaly baseline statistics use 8.
Currencies are never aggregated together: every summary/alert/anomaly is keyed by currency.

## Error format

```json
{ "error": "message", "status_code": 422, "details": [ ... ] }
```

## Catalog

### `GET /api/v1/organizations`
Organizations visible to the caller.

### `GET /api/v1/cost-centers`
### `GET /api/v1/accounts`
Accounts carry `org_id` and `cost_center_id`.

### `POST /api/v1/admin/organizations` *(admin)*
```json
{ "external_id": "org-acme", "name": "Acme" }
```

### `POST /api/v1/admin/cost-centers` *(admin)*
```json
{ "org_external_id": "org-acme", "code": "cc-platform", "name": "Platform" }
```

### `POST /api/v1/admin/accounts` *(admin)*
```json
{ "org_external_id": "org-acme",
  "cost_center_code": "cc-platform",
  "external_id": "acct-001", "name": "Prod billing account" }
```

### `POST /api/v1/admin/users` *(admin)*
```json
{ "username": "alice", "role": "analyst" }
```
Response contains the API token **once**:
```json
{ "username": "alice", "api_token": "clt_…" }
```
`role` is one of `admin|analyst|viewer`.

### `POST /api/v1/admin/users/{username}/grants` *(admin)*
```json
{ "org_external_id": "org-acme" }
```

## CSV import

### `POST /api/v1/accounts/{externalID}/imports`
Requires **analyst** (granted org) or **admin**. Body is either:

- raw CSV with `Content-Type: text/csv` (optional `X-Filename` header), or
- `multipart/form-data` with a `file` field ending `.csv`.

Header must be exactly:

```
resource_id,service,date,currency,amount
```

- `date` — `YYYY-MM-DD`
- `currency` — 3 uppercase letters (e.g. `USD`)
- `amount` — exact fixed-point decimal, at most 6 fractional digits, no exponents
- at most **5000 data rows** per batch

Semantics:

- Dedup key is `(account, resource_id, date)`. Exact duplicate rows
  (identical service/date/currency/amount) are counted as `duplicate_rows`.
- Same key with different content is a **409 conflict**; the response lists
  the conflicting keys with existing values and CSV **line numbers**, and the
  **entire batch is rolled back** (no rows, no batch record, no summary
  changes).
- Validation failures return **422** with per-row errors including line
  numbers; nothing is imported.

Success:

```json
{ "batch_id": 12, "total_rows": 500, "inserted_rows": 480,
  "duplicate_rows": 20, "new_alert_ids": [] }
```

Conflict example:

```json
{ "error": "content conflict for existing key(s); entire batch rolled back",
  "status_code": 409,
  "details": { "conflicts": [
    { "key": "i-0f1@2026-08-01", "resource_id": "i-0f1",
      "date": "2026-08-01", "currency": "USD",
      "existing_service": "compute", "existing_amount": "42.175000",
      "lines": [7] }
  ] } }
```

Example:

```bash
curl -sS -X POST "http://localhost:8080/api/v1/accounts/acct-001/imports" \
  -H "Authorization: Bearer $ANALYST_TOKEN" \
  -H "Content-Type: text/csv" \
  --data-binary @testdata/sample_billing.csv
```

## Batches

### `GET /api/v1/batches?limit=&offset=`
Import history scoped to visible organizations.

## Records & export

### `GET /api/v1/records?from=&to=&currency=&limit=&offset=`
Billing detail lines, isolated to accounts the caller may see.
`from`/`to` are `YYYY-MM-DD` inclusive.

### `GET /api/v1/exports/records.csv?from=&to=&currency=`
Streams the same rows the caller may see as CSV (paginated server-side).
Columns: `account_external_id,resource_id,service,date,currency,amount`.

## Summaries

### `GET /api/v1/summaries/daily?scope=account|cost_center&from=&to=`
### `GET /api/v1/summaries/monthly?scope=account|cost_center&from=&to=`

Rows:

```json
{ "scope": "account", "account_id": 3,
  "date": "2026-08-01", "currency": "USD",
  "total_amount": "57.625000", "record_count": 3 }
```

Monthly `date` is the first day of the month. Totals never mix currencies.
Late imports recompute only the touched day/month slices.

## Budgets & alerts

### `POST /api/v1/admin/budgets` *(admin)*
```json
{ "cost_center_id": 1, "currency": "USD",
  "month": "2026-09", "monthly_limit": "1000.00" }
```
Creates a new immutable **version**, deactivating the previous one.
Alerts always retain their budget version (`budget_version` in responses),
so historical evidence is preserved across budget changes.

### `GET /api/v1/budgets`
All versions for visible cost centers (active and superseded).

### `GET /api/v1/budget-alerts`
Alerts at thresholds **50 / 75 / 90 / 100 %** of month-to-date spend vs the
active budget version, per cost center and currency. Each
`(budget_version, threshold)` fires at most once, at its first crossing:

```json
{ "id": 4, "budget_id": 9, "budget_version": 1, "cost_center_id": 1,
  "threshold_pct": 75, "spent_amount": "760.000000",
  "month": "2026-09", "currency": "USD",
  "triggered_at": "2026-09-22T10:00:00Z" }
```

## Anomaly detection

### `GET /api/v1/anomalies?status=anomalous`
Latest evaluation version per `(account, date, currency)`.

Baseline for a target day **D** = the **30 complete calendar days D-30…D-1**
(the current day never participates). Zero-spend days count as `0`.

`threshold = mean + 2 × population_stddev` over the 30 days.

Statuses:

| status | meaning |
|---|---|
| `anomalous` | actual > threshold |
| `normal` | actual ≤ threshold, variance > 0 |
| `zero_variance_below` | variance = 0 and actual = mean (a flat, equal-spend day is not an anomaly) |
| `insufficient_history` | fewer than 30 days between the account's first record and D |

With zero variance, any value strictly greater than the constant mean is
`anomalous`.

### `GET /api/v1/anomalies/versions?account_id=&date=&currency=`
All stored versions for one key. Late data that shifts a baseline appends a
new version; prior versions and alerts remain.

## Maintenance

### `POST /api/v1/admin/rebuild` *(admin)*
Truncates and fully recomputes all daily/monthly summaries from
`billing_records` and re-runs anomaly evaluations, in one transaction under
the same advisory lock imports use. Imports block while it runs and then
commit against the rebuilt state — no lost or double-counted rows. Budget
alerts are never rewritten. Returns `{"rebuild_event_id": …, "status": "completed"}`.

## Health

### `GET /healthz` → `200 {"status":"ok"}` (no auth)
