-- name: CreateBudgetVersion :one
INSERT INTO budgets (cost_center_id, currency, month, version, monthly_limit, created_by)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING id, cost_center_id, currency, month, version, monthly_limit,
          active, created_by, created_at;

-- name: GetActiveBudget :one
SELECT id, cost_center_id, currency, month, version, monthly_limit,
       active, created_by, created_at
FROM budgets
WHERE cost_center_id = $1 AND currency = $2 AND month = $3 AND active;

-- name: DeactivateBudget :exec
UPDATE budgets SET active = FALSE WHERE id = $1;

-- name: NextBudgetVersion :one
SELECT COALESCE(max(version), 0) + 1
FROM budgets
WHERE cost_center_id = $1 AND currency = $2 AND month = $3;

-- name: ListBudgetVersions :many
SELECT id, cost_center_id, currency, month, version, monthly_limit,
       active, created_by, created_at
FROM budgets
WHERE (cardinality(COALESCE(@cc_ids, ARRAY[]::bigint[])) = 0 OR cost_center_id = ANY(@cc_ids))
ORDER BY month DESC, cost_center_id, currency, version DESC;

-- name: InsertBudgetAlert :execrows
INSERT INTO budget_alerts
    (budget_id, threshold_pct, spent_amount, month, currency,
     triggered_by_batch)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (budget_id, threshold_pct) DO NOTHING;

-- name: ListBudgetAlerts :many
SELECT a.id, a.budget_id, a.threshold_pct, a.spent_amount, a.month,
       a.currency, a.triggered_by_batch, a.triggered_at,
       b.cost_center_id, b.version AS budget_version
FROM budget_alerts a
JOIN budgets b ON b.id = a.budget_id
WHERE (cardinality(COALESCE(@cc_ids, ARRAY[]::bigint[])) = 0 OR b.cost_center_id = ANY(@cc_ids))
ORDER BY a.triggered_at DESC;
