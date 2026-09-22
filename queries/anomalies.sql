-- name: InsertAnomalyEvaluation :exec
INSERT INTO anomaly_evaluations
    (account_id, usage_date, currency, version, status, actual_amount,
     baseline_mean, baseline_std, threshold_amount, baseline_start,
     baseline_end, baseline_days, created_by_batch)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13);

-- name: NextAnomalyVersion :one
SELECT COALESCE(max(version), 0) + 1
FROM anomaly_evaluations
WHERE account_id = $1 AND usage_date = $2 AND currency = $3;

-- name: GetLatestAnomaly :one
SELECT id, account_id, usage_date, currency, version, status, actual_amount,
       baseline_mean, baseline_std, threshold_amount, baseline_start,
       baseline_end, baseline_days, created_by_batch, created_at
FROM anomaly_evaluations
WHERE account_id = $1 AND usage_date = $2 AND currency = $3
ORDER BY version DESC
LIMIT 1;

-- name: ListAnomalies :many
SELECT e.id, e.account_id, e.usage_date, e.currency, e.version, e.status,
       e.actual_amount, e.baseline_mean, e.baseline_std, e.threshold_amount,
       e.baseline_start, e.baseline_end, e.baseline_days,
       e.created_by_batch, e.created_at
FROM anomaly_evaluations e
WHERE (cardinality(COALESCE(@account_ids, ARRAY[]::bigint[])) = 0 OR e.account_id = ANY(@account_ids))
  AND e.version = (
      SELECT max(e2.version) FROM anomaly_evaluations e2
      WHERE e2.account_id = e.account_id
        AND e2.usage_date = e.usage_date
        AND e2.currency = e.currency)
  AND (@only_anomalous::boolean = FALSE OR e.status = 'anomalous')
ORDER BY e.usage_date DESC, e.account_id, e.currency;

-- name: ListAnomalyVersions :many
SELECT id, account_id, usage_date, currency, version, status, actual_amount,
       baseline_mean, baseline_std, threshold_amount, baseline_start,
       baseline_end, baseline_days, created_by_batch, created_at
FROM anomaly_evaluations
WHERE account_id = $1 AND usage_date = $2 AND currency = $3
ORDER BY version;

-- name: CreateRebuildEvent :one
INSERT INTO rebuild_events (status, started_by) VALUES ($1, $2)
RETURNING id, status, started_by, started_at, finished_at;

-- name: FinishRebuildEvent :exec
UPDATE rebuild_events
SET status = $2, finished_at = now() WHERE id = $1;
