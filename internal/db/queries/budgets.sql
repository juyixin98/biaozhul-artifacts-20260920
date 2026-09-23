-- name: CreateBudgetVersion :one
INSERT INTO budget_versions (cost_center_id, currency, period, amount, version, created_by)
SELECT $1, $2, $3, $4,
       COALESCE((SELECT MAX(version) FROM budget_versions
                 WHERE cost_center_id = $1 AND currency = $2 AND period = $3), 0) + 1,
       $5
RETURNING *;

-- Current (highest-version) budget per CC/currency/period, optionally filtered.
-- name: ListCurrentBudgetsScoped :many
SELECT DISTINCT ON (bv.cost_center_id, bv.currency, bv.period) bv.*
FROM budget_versions bv
JOIN cost_centers cc ON cc.id = bv.cost_center_id
WHERE cc.org_id = ANY($1::uuid[])
  AND (sqlc.narg('cost_center_id')::uuid IS NULL OR bv.cost_center_id = @cost_center_id)
  AND (sqlc.narg('currency')::char(3) IS NULL OR bv.currency = @currency)
  AND (sqlc.narg('period')::date IS NULL OR bv.period = @period)
ORDER BY bv.cost_center_id, bv.currency, bv.period, bv.version DESC, bv.created_at DESC;

-- name: ListBudgetVersionsScoped :many
SELECT bv.* FROM budget_versions bv
JOIN cost_centers cc ON cc.id = bv.cost_center_id
WHERE cc.org_id = ANY($1::uuid[])
  AND bv.cost_center_id = $2
  AND bv.currency = $3
  AND bv.period = $4
ORDER BY bv.version DESC;

-- Current budgets for a set of (cost center, currency, period) evaluation slices.
-- name: CurrentBudgetsForSlices :many
SELECT DISTINCT ON (bv.cost_center_id, bv.currency, bv.period)
       bv.id, bv.cost_center_id, bv.currency, bv.period, bv.amount, bv.version
FROM budget_versions bv
WHERE (bv.cost_center_id, bv.currency, bv.period) IN (
    SELECT UNNEST($1::text[])::uuid, UNNEST($2::text[])::char(3), UNNEST($3::text[])::date
)
ORDER BY bv.cost_center_id, bv.currency, bv.period, bv.version DESC, bv.created_at DESC;

-- All current budgets (rebuild path).
-- name: AllCurrentBudgets :many
SELECT DISTINCT ON (cost_center_id, currency, period)
       id, cost_center_id, currency, period, amount, version
FROM budget_versions
ORDER BY cost_center_id, currency, period, version DESC, created_at DESC;

-- First-crossing alert insertion; the unique constraint makes it idempotent.
-- name: InsertBudgetAlert :execrows
INSERT INTO budget_alerts
    (budget_version_id, cost_center_id, currency, period, threshold, actual_amount, triggered_by_run)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (budget_version_id, threshold) DO NOTHING;

-- name: ListBudgetAlertsScoped :many
SELECT ba.* FROM budget_alerts ba
JOIN cost_centers cc ON cc.id = ba.cost_center_id
WHERE cc.org_id = ANY($1::uuid[])
  AND (sqlc.narg('cost_center_id')::uuid IS NULL OR ba.cost_center_id = @cost_center_id)
  AND (sqlc.narg('currency')::char(3) IS NULL OR ba.currency = @currency)
  AND (sqlc.narg('period')::date IS NULL OR ba.period = @period)
ORDER BY ba.period DESC, ba.created_at DESC;
