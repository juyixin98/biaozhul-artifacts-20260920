-- name: InsertChannelEvent :exec
INSERT INTO channel_events (merchant_id, event_date, event_type, payment_id, refund_id, gross_amount, fee_delta)
VALUES ($1, $2, $3, $4, $5, $6, $7);

-- name: ChannelCaptureTotals :one
SELECT COALESCE(SUM(gross_amount),0)::bigint AS gross,
       COALESCE(SUM(fee_delta),0)::bigint   AS fees,
       COUNT(*)::int                        AS cnt
FROM channel_events
WHERE merchant_id = $1 AND event_date = $2 AND event_type = 'capture';

-- name: ChannelRefundTotals :one
SELECT COALESCE(SUM(gross_amount),0)::bigint AS gross,
       COALESCE(SUM(fee_delta),0)::bigint   AS fees,
       COUNT(*)::int                        AS cnt
FROM channel_events
WHERE merchant_id = $1 AND event_date = $2 AND event_type = 'refund';

-- name: InternalCaptureTotals :one
SELECT COALESCE(SUM(captured_amount),0)::bigint AS gross,
       COALESCE(SUM(fee_amount),0)::bigint      AS fees,
       COUNT(*)::int                            AS cnt
FROM payments
WHERE merchant_id = $1 AND (captured_at AT TIME ZONE 'UTC')::date = sqlc.arg(day)::date
  AND captured_at IS NOT NULL;

-- name: InternalRefundTotals :one
SELECT COALESCE(SUM(r.amount),0)::bigint     AS gross,
       COALESCE(SUM(r.fee_refund),0)::bigint AS fees,
       COUNT(*)::int                         AS cnt
FROM refunds r
WHERE r.merchant_id = $1 AND (r.created_at AT TIME ZONE 'UTC')::date = sqlc.arg(day)::date;

-- name: UpsertReconRun :one
INSERT INTO reconciliation_runs (merchant_id, run_date, status)
VALUES ($1, $2, 'running')
ON CONFLICT (merchant_id, run_date) DO NOTHING
RETURNING *;

-- name: GetReconRun :one
SELECT * FROM reconciliation_runs WHERE merchant_id = $1 AND run_date = $2;

-- name: GetReconRunForUpdate :one
SELECT * FROM reconciliation_runs WHERE merchant_id = $1 AND run_date = $2 FOR UPDATE;

-- name: CompleteReconRun :one
UPDATE reconciliation_runs
SET status = 'completed', completed_at = now(), detail_summary = $3
WHERE id = $1 AND merchant_id = $2 AND status = 'running'
RETURNING *;

-- name: FailReconRun :exec
UPDATE reconciliation_runs SET status = 'failed' WHERE id = $1 AND status = 'running';

-- name: InsertReconItem :exec
INSERT INTO reconciliation_items (run_id, check_name, expected, actual, difference, severity, detail)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (run_id, check_name) DO NOTHING;

-- name: DeleteReconItems :exec
DELETE FROM reconciliation_items WHERE run_id = $1;

-- name: ListReconItems :many
SELECT * FROM reconciliation_items WHERE run_id = $1 ORDER BY check_name;

-- name: ListReconRunsForMerchant :many
SELECT * FROM reconciliation_runs WHERE merchant_id = $1
ORDER BY run_date DESC LIMIT $2 OFFSET $3;

-- name: ListAllReconRuns :many
SELECT rr.*, m.name AS merchant_name
FROM reconciliation_runs rr JOIN merchants m ON m.id = rr.merchant_id
ORDER BY run_date DESC, m.name LIMIT $1 OFFSET $2;

-- name: UnbalancedPostingCount :one
SELECT COUNT(*)::int FROM v_unbalanced_postings;

-- name: MerchantsWithCaptureOn :many
SELECT DISTINCT merchant_id FROM payments
WHERE captured_at IS NOT NULL AND (captured_at AT TIME ZONE 'UTC')::date = sqlc.arg(day)::date;

-- name: MerchantsWithRefundOn :many
SELECT DISTINCT merchant_id FROM refunds
WHERE (created_at AT TIME ZONE 'UTC')::date = sqlc.arg(day)::date;
