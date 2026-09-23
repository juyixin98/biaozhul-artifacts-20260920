# Design notes

## Exact money

Every amount is `NUMERIC(20,6)` (≤14 integer + 6 fraction digits). Parsing
(`internal/money`) rejects scientific notation, >6 fraction digits, >14 integer
digits, and negatives; rows reach PostgreSQL as canonical text. Aggregation is
done by PostgreSQL `numeric`; mean/std in Go use `shopspring/decimal` (32-digit
division precision, Newton–Raphson square root) — no `float64` anywhere in the
money path. JSON carries amounts as strings.

Currencies are a grouping key in every aggregate and every budget; totals in
different currencies are never added.

## Dedup, conflict, atomic batch

`costs.UNIQUE(account_id, resource_id, cost_date)` is the dedup key. On import:

1. Parse whole file first (`≤5000` data rows); any parse error aborts with the CSV line.
2. Inside one transaction: resolve accounts (must belong to the authorized org),
   auto-register new resources, then compare every row with the committed rows
   for its key/date range.
   - key exists, content identical → counted as a duplicate, not inserted;
   - key exists, `amount`/`currency`/`service` differs → `content_conflict`
     with the file line; the transaction rolls back (including earlier valid rows).
3. Succeeded batches write a `cost_imports` audit row that the new costs reference;
   failed batches persist a failed audit row in a separate post-rollback
   transaction, so rejected files remain visible with their line number.

## Incremental aggregates vs. rebuild

Four summary tables (daily/monthly × account/cost-center). An import deletes and
recomputes only the slices it can affect: daily rows for its day range, monthly
rows for every month the range spans — recomputed over the **full month span**,
so a late bill landing in an old month corrects that month without touching
others. A full rebuild truncates the four tables and recomputes everything from
raw `costs`. Integration tests assert both paths produce identical rows.

Both paths take the same `pg_advisory_xact_lock` for their whole transaction, so
they serialize globally: a concurrent duplicate import cannot double-aggregate
(it waits, then sees the first import's rows), and an import racing a rebuild
commits either fully before truncate or fully after recompute — never lost, never
counted twice.

## Budgets

`budget_versions` is append-only: a revision inserts a new version; existing
alerts keep pointing at the version that justified them. Alerts have
`UNIQUE(budget_version_id, threshold)` and are inserted with
`ON CONFLICT DO NOTHING`, making "first crossing only" idempotent across
re-imports and rebuilds. Crossing uses `actual/budget >= threshold` (the exact
boundary counts).

## Anomaly baseline

Baseline = the 30 complete days immediately before the target day, per
(account, currency); zero-spend days contribute explicit zeros. "Insufficient
history" is defined by **account existence** (`accounts.created_at` after the
window start), not by sparse spending — an existing account that spent nothing is
a legitimate zero baseline. Zero variance means `threshold = mean` and the
comparison is strictly `>`, so a constant series never alerts on itself.

Each import creates an `anomaly_runs` row and appends evaluations for its
imported days plus the following 30 days (whose windows the new data enters).
Late data therefore produces a new evaluation version for affected days while
old evaluations stay queryable via `/anomalies/history`; `/anomalies` exposes
only the latest verdict per key.

## Isolation

Authorization resolves an API key (SHA-256 hashed at rest) to role + org grants;
admins are expanded to the full org list. Every detail/summary/export query
filters with `org_id = ANY(<granted ids>)` joined through `accounts` /
`cost_centers`, so isolation is enforced in SQL, not in presentation code.
