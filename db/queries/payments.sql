-- name: CreatePayment :one
INSERT INTO payments (
    merchant_id, idempotency_key, status, authorized_amount,
    fee_bps, fee_fixed, external_ref, expires_at
) VALUES ($1, $2, 'authorized', $3, $4, $5, $6,
          now() + make_interval(secs => $7))
RETURNING *;

-- name: GetPayment :one
SELECT * FROM payments WHERE id = $1 AND merchant_id = $2;

-- name: GetPaymentForUpdate :one
SELECT * FROM payments WHERE id = $1 AND merchant_id = $2 FOR UPDATE;

-- name: CapturePayment :one
UPDATE payments
SET status = 'captured',
    captured_amount = $3,
    fee_amount = $4,
    captured_at = now()
WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
RETURNING *;

-- name: VoidPayment :one
UPDATE payments
SET status = 'voided', voided_at = now()
WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
RETURNING *;

-- name: ApplyRefund :one
UPDATE payments
SET refunded_amount = refunded_amount + $3,
    refunded_fee    = refunded_fee + $4,
    status = CASE WHEN settlement_batch_id IS NOT NULL THEN 'settled'
                  WHEN refunded_amount + $3 = captured_amount
                  THEN 'refunded' ELSE 'partially_refunded' END
WHERE id = $1 AND merchant_id = $2
RETURNING *;

-- name: MarkPaymentSettled :many
UPDATE payments
SET settlement_batch_id = $3,
    status = 'settled'
WHERE id = ANY($1::uuid[]) AND merchant_id = $2
  AND status IN ('captured','partially_refunded')
RETURNING id;

-- name: ListUnsettledCapturedPayments :many
SELECT * FROM payments
WHERE merchant_id = $1
  AND captured_at IS NOT NULL
  AND captured_at < ($2::timestamptz)
  AND settlement_batch_id IS NULL
  AND status IN ('captured','partially_refunded')
ORDER BY captured_at
LIMIT $3
FOR UPDATE SKIP LOCKED;

-- name: CountUnsettledCapturedPayments :one
SELECT COUNT(*)::int FROM payments
WHERE merchant_id = $1
  AND captured_at IS NOT NULL
  AND captured_at < ($2::timestamptz)
  AND settlement_batch_id IS NULL
  AND status IN ('captured','partially_refunded');

-- name: CreateRefund :one
INSERT INTO refunds (payment_id, merchant_id, idempotency_key, amount, fee_refund, reason)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: GetRefundByIdemKey :one
SELECT * FROM refunds WHERE merchant_id = $1 AND idempotency_key = $2;

-- name: CreateIdempotentRequest :one
INSERT INTO idempotent_requests (merchant_id, idem_key, route, request_hash, status_code, response_body)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: GetIdempotentRequest :one
SELECT * FROM idempotent_requests WHERE merchant_id = $1 AND idem_key = $2;
