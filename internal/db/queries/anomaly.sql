-- name: CreateAnomalyRun :one
INSERT INTO anomaly_runs (kind, import_id)
VALUES ($1, $2)
RETURNING *;

-- name: ListLatestAnomalyAlertsScoped :many
SELECT la.id, la.run_id, la.account_id, la.currency, la.cost_date,
       la.actual_amount, la.baseline_mean, la.baseline_std, la.threshold, la.created_at
FROM latest_anomaly_alerts la
JOIN accounts a ON a.id = la.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR la.account_id = @account_id)
  AND (sqlc.narg('currency')::char(3) IS NULL OR la.currency = @currency)
  AND (sqlc.narg('date_from')::date IS NULL OR la.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR la.cost_date <= @date_to)
ORDER BY la.cost_date DESC, la.account_id;

-- Full versioned history: old evaluations stay visible after late data changes a baseline.
-- name: ListAnomalyHistoryScoped :many
SELECT e.id, e.run_id, r.kind AS run_kind, e.account_id, e.currency, e.cost_date,
       e.actual_amount, e.baseline_mean, e.baseline_std, e.threshold, e.status, e.created_at
FROM anomaly_evaluations e
JOIN anomaly_runs r ON r.id = e.run_id
JOIN accounts a ON a.id = e.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR e.account_id = @account_id)
  AND (sqlc.narg('currency')::char(3) IS NULL OR e.currency = @currency)
  AND (sqlc.narg('date_from')::date IS NULL OR e.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR e.cost_date <= @date_to)
ORDER BY e.cost_date DESC, a.id, e.created_at DESC
LIMIT 10000;
