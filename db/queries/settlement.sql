-- name: UpsertSettlementBatch :one
INSERT INTO settlement_batches (merchant_id, batch_date, status, locked_by)
VALUES (sqlc.arg(merchant_id), sqlc.arg(batch_date), 'processing', sqlc.arg(locked_by))
ON CONFLICT (merchant_id, batch_date) DO NOTHING
RETURNING *;

-- name: GetSettlementBatch :one
SELECT * FROM settlement_batches WHERE id = $1;

-- name: GetSettlementBatchForUpdate :one
SELECT * FROM settlement_batches WHERE merchant_id = sqlc.arg(merchant_id) AND batch_date = sqlc.arg(batch_date) FOR UPDATE;

-- name: CompleteSettlementBatch :one
UPDATE settlement_batches
SET status = 'completed',
    gross_captured = sqlc.arg(gross_captured),
    total_fees = sqlc.arg(total_fees),
    total_refunds = sqlc.arg(total_refunds),
    refunded_fees = sqlc.arg(refunded_fees),
    net_amount = sqlc.arg(net_amount),
    payment_count = sqlc.arg(payment_count),
    completed_at = now()
WHERE id = sqlc.arg(id) AND merchant_id = sqlc.arg(merchant_id) AND status = 'processing'
RETURNING *;

-- name: FailSettlementBatch :exec
UPDATE settlement_batches SET status = 'failed' WHERE id = $1 AND status = 'processing';

-- name: ListSettlementBatches :many
SELECT * FROM settlement_batches WHERE merchant_id = $1
ORDER BY batch_date DESC LIMIT $2 OFFSET $3;

-- name: ListAllSettlementBatches :many
SELECT sb.*, m.name AS merchant_name
FROM settlement_batches sb JOIN merchants m ON m.id = sb.merchant_id
ORDER BY batch_date DESC, m.name LIMIT $1 OFFSET $2;

-- name: ListPaymentsInBatch :many
SELECT * FROM payments WHERE settlement_batch_id = $1 ORDER BY captured_at;

-- name: PaymentsForBatchDay :many
-- Locks the exact set of payments that belong in this (merchant, day) batch.
-- SKIP LOCKED lets two workers on different merchants/days proceed in parallel.
SELECT * FROM payments
WHERE merchant_id = sqlc.arg(merchant_id)
  AND (captured_at AT TIME ZONE 'UTC')::date = sqlc.arg(batch_date)::date
  AND settlement_batch_id IS NULL
  AND captured_at IS NOT NULL
ORDER BY captured_at
LIMIT sqlc.arg(row_limit)
FOR UPDATE SKIP LOCKED;

-- name: CountPaymentsForBatchDay :one
SELECT COUNT(*)::int FROM payments
WHERE merchant_id = sqlc.arg(merchant_id)
  AND (captured_at AT TIME ZONE 'UTC')::date = sqlc.arg(batch_date)::date
  AND settlement_batch_id IS NULL
  AND captured_at IS NOT NULL;

-- name: AggregatePaymentsForBatchDay :one
SELECT
    COUNT(*)::int                                   AS payment_count,
    COALESCE(SUM(captured_amount),0)::bigint        AS gross_captured,
    COALESCE(SUM(fee_amount),0)::bigint             AS total_fees,
    COALESCE(SUM(refunded_amount),0)::bigint        AS total_refunds,
    COALESCE(SUM(refunded_fee),0)::bigint           AS refunded_fees,
    COALESCE(SUM(captured_amount - refunded_amount
                 - fee_amount + refunded_fee),0)::bigint AS net_payable
FROM payments
WHERE merchant_id = sqlc.arg(merchant_id)
  AND (captured_at AT TIME ZONE 'UTC')::date = sqlc.arg(batch_date)::date
  AND settlement_batch_id IS NULL
  AND captured_at IS NOT NULL;

-- name: MarkBatchPaymentsSettled :exec
UPDATE payments
SET settlement_batch_id = sqlc.arg(batch_id),
    status = CASE WHEN refunded_amount = captured_amount THEN status ELSE 'settled' END
WHERE merchant_id = sqlc.arg(merchant_id)
  AND (captured_at AT TIME ZONE 'UTC')::date = sqlc.arg(batch_date)::date
  AND settlement_batch_id IS NULL
  AND captured_at IS NOT NULL;

-- name: ListMerchantDaysToSettle :many
-- One row per (merchant, capture-day) that still has un-batched captures,
-- restricted to days entirely before the cutoff (day end <= cutoff), so the
-- worker never settles a day that can still receive events.
SELECT DISTINCT p.merchant_id,
       m.name AS merchant_name,
       (p.captured_at AT TIME ZONE 'UTC')::date AS day
FROM payments p
JOIN merchants m ON m.id = p.merchant_id
WHERE p.captured_at IS NOT NULL AND p.settlement_batch_id IS NULL
  AND (p.captured_at AT TIME ZONE 'UTC')::date < (sqlc.arg(cutoff)::timestamptz AT TIME ZONE 'UTC')::date
ORDER BY day, p.merchant_id;
