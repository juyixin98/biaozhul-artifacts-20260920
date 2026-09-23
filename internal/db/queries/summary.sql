-- ===== Incremental: delete affected slices =====

-- name: DeleteDailyAccountSlice :exec
DELETE FROM daily_account_summary
WHERE cost_date BETWEEN $1 AND $2
  AND account_id = ANY($3::uuid[]);

-- name: DeleteMonthlyAccountSlice :exec
DELETE FROM monthly_account_summary
WHERE period = ANY($1::date[])
  AND account_id = ANY($2::uuid[]);

-- name: DeleteDailyCostCenterSlice :exec
DELETE FROM daily_cost_center_summary
WHERE cost_date BETWEEN $1 AND $2
  AND cost_center_id = ANY($3::uuid[]);

-- name: DeleteMonthlyCostCenterSlice :exec
DELETE FROM monthly_cost_center_summary
WHERE period = ANY($1::date[])
  AND cost_center_id = ANY($2::uuid[]);

-- ===== Rebuild: wipe all derived data =====

-- name: TruncateSummaries :exec
TRUNCATE daily_account_summary,
          monthly_account_summary,
          daily_cost_center_summary,
          monthly_cost_center_summary;

-- ===== Recompute aggregates from raw costs (restricted to affected accounts) =====

-- name: RecomputeDailyAccount :exec
INSERT INTO daily_account_summary (account_id, currency, cost_date, total, row_count)
SELECT c.account_id, c.currency, c.cost_date, SUM(c.amount), COUNT(*)
FROM costs c
WHERE c.cost_date BETWEEN $1 AND $2
  AND c.account_id = ANY($3::uuid[])
GROUP BY c.account_id, c.currency, c.cost_date;

-- name: RecomputeMonthlyAccount :exec
INSERT INTO monthly_account_summary (account_id, currency, period, total, row_count)
SELECT c.account_id, c.currency, date_trunc('month', c.cost_date)::date, SUM(c.amount), COUNT(*)
FROM costs c
WHERE c.cost_date BETWEEN $1 AND $2
  AND c.account_id = ANY($3::uuid[])
GROUP BY c.account_id, c.currency, date_trunc('month', c.cost_date)::date;

-- name: RecomputeDailyCostCenter :exec
INSERT INTO daily_cost_center_summary (cost_center_id, currency, cost_date, total, row_count)
SELECT a.cost_center_id, c.currency, c.cost_date, SUM(c.amount), COUNT(*)
FROM costs c JOIN accounts a ON a.id = c.account_id
WHERE c.cost_date BETWEEN $1 AND $2
  AND c.account_id = ANY($3::uuid[])
GROUP BY a.cost_center_id, c.currency, c.cost_date;

-- name: RecomputeMonthlyCostCenter :exec
INSERT INTO monthly_cost_center_summary (cost_center_id, currency, period, total, row_count)
SELECT a.cost_center_id, c.currency, date_trunc('month', c.cost_date)::date,
       SUM(c.amount), COUNT(*)
FROM costs c JOIN accounts a ON a.id = c.account_id
WHERE c.cost_date BETWEEN $1 AND $2
  AND c.account_id = ANY($3::uuid[])
GROUP BY a.cost_center_id, c.currency, date_trunc('month', c.cost_date)::date;

-- ===== Scoped reads =====

-- name: ListDailyAccountScoped :many
SELECT s.* FROM daily_account_summary s
JOIN accounts a ON a.id = s.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR s.account_id = @account_id)
  AND (sqlc.narg('date_from')::date IS NULL OR s.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR s.cost_date <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR s.currency = @currency)
ORDER BY s.cost_date, s.account_id;

-- name: ListMonthlyAccountScoped :many
SELECT s.* FROM monthly_account_summary s
JOIN accounts a ON a.id = s.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR s.account_id = @account_id)
  AND (sqlc.narg('date_from')::date IS NULL OR s.period >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR s.period <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR s.currency = @currency)
ORDER BY s.period, s.account_id;

-- name: ListDailyCostCenterScoped :many
SELECT s.* FROM daily_cost_center_summary s
JOIN cost_centers cc ON cc.id = s.cost_center_id
WHERE cc.org_id = ANY($1::uuid[])
  AND (sqlc.narg('cost_center_id')::uuid IS NULL OR s.cost_center_id = @cost_center_id)
  AND (sqlc.narg('date_from')::date IS NULL OR s.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR s.cost_date <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR s.currency = @currency)
ORDER BY s.cost_date, s.cost_center_id;

-- name: ListMonthlyCostCenterScoped :many
SELECT s.* FROM monthly_cost_center_summary s
JOIN cost_centers cc ON cc.id = s.cost_center_id
WHERE cc.org_id = ANY($1::uuid[])
  AND (sqlc.narg('cost_center_id')::uuid IS NULL OR s.cost_center_id = @cost_center_id)
  AND (sqlc.narg('date_from')::date IS NULL OR s.period >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR s.period <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR s.currency = @currency)
ORDER BY s.period, s.cost_center_id;

-- ===== Budget evaluation: month totals by CC/currency/period slices =====

-- All currency totals for given cost centers and one month (budget evaluation).
-- name: MonthlyCostCenterTotalsForPeriod :many
SELECT cost_center_id, currency, period, total, row_count
FROM monthly_cost_center_summary
WHERE period = $1
  AND cost_center_id = ANY($2::uuid[]);

