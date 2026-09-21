-- name: CreateBatch :one
INSERT INTO settlement_batches (merchant_id, batch_date)
VALUES ($1, sqlc.arg('batch_date')::date)
RETURNING *;

-- name: GetBatchByMerchantDate :one
SELECT * FROM settlement_batches WHERE merchant_id = $1 AND batch_date = sqlc.arg('batch_date')::date;

-- name: GetBatchByMerchantDateForUpdate :one
SELECT * FROM settlement_batches WHERE merchant_id = $1 AND batch_date = sqlc.arg('batch_date')::date FOR UPDATE;

-- name: MarkBatchDone :one
UPDATE settlement_batches
SET status = 'done', total_cents = $2, fee_cents = $3, net_cents = $4, completed_at = $5
WHERE id = $1
RETURNING *;

-- name: AddBatchItem :exec
INSERT INTO settlement_items (batch_id, payment_id, gross_cents, fee_cents, net_cents)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (payment_id) DO NOTHING;

-- name: PaymentSettledInBatch :one
SELECT EXISTS(
    SELECT 1 FROM settlement_items WHERE payment_id = $1
) AS settled;

-- name: GetBatch :one
SELECT * FROM settlement_batches WHERE id = $1;

-- name: ListBatchesByMerchant :many
SELECT * FROM settlement_batches WHERE merchant_id = $1 ORDER BY batch_date DESC;

-- name: ListBatchItems :many
SELECT * FROM settlement_items WHERE batch_id = $1 ORDER BY id;

-- name: SumBatchItems :one
SELECT COALESCE(SUM(gross_cents),0)::BIGINT AS gross,
       COALESCE(SUM(fee_cents),0)::BIGINT   AS fee,
       COALESCE(SUM(net_cents),0)::BIGINT   AS net,
       COUNT(*)::BIGINT                      AS n
FROM settlement_items WHERE batch_id = $1;

-- Payments eligible for a day's batch: captured, partially refunded or fully
-- refunded BEFORE settlement (settled_at IS NULL distinguishes them from
-- post-settlement refunds), created before the end of the batch date.
-- Rows are locked so only one batch process can claim them.
-- name: ListSettleablePayments :many
SELECT transactions.* FROM transactions
WHERE transactions.merchant_id = $1
  AND transactions.created_at <= $2
  AND transactions.settled_at IS NULL
  AND transactions.status IN ('captured','partially_refunded','refunded')
ORDER BY transactions.created_at, transactions.id
FOR UPDATE OF transactions SKIP LOCKED;

-- name: MarkPaymentSettled :one
UPDATE transactions
SET status = CASE WHEN refunded_cents >= captured_cents AND refunded_cents > 0
                    THEN 'refunded'
                  WHEN refunded_cents > 0 THEN 'partially_refunded'
                  ELSE 'settled' END,
    settled_cents = captured_cents - refunded_cents,
    settled_at = $2,
    refund_deadline = $2::timestamptz + INTERVAL '90 days'
WHERE id = $1
RETURNING *;
