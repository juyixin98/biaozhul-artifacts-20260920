-- name: CreatePayment :one
INSERT INTO transactions (
    merchant_id, amount_cents, card_last4, auth_expires_at, created_at
) VALUES (
    sqlc.arg('merchant_id'),
    sqlc.arg('amount_cents'),
    sqlc.arg('card_last4'),
    sqlc.arg('auth_expires_at')::timestamptz,
    sqlc.arg('created_at')::timestamptz
)
RETURNING *;

-- name: GetPayment :one
SELECT * FROM transactions WHERE id = $1;

-- name: GetPaymentForUpdate :one
SELECT * FROM transactions WHERE id = $1 FOR UPDATE;

-- name: ListPaymentsByMerchant :many
SELECT * FROM transactions
WHERE merchant_id = $1
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;

-- name: MarkCaptured :one
UPDATE transactions
SET status = 'captured',
    captured_cents = $2,
    fee_cents = $3,
    captured_at = $4
WHERE id = $1
RETURNING *;

-- name: ApplyRefund :one
UPDATE transactions
SET status = CASE WHEN refunded_cents + $2 >= captured_cents THEN 'refunded'
                  ELSE 'partially_refunded' END,
    refunded_cents = refunded_cents + $2,
    refunded_fee_cents = refunded_fee_cents + $3
WHERE id = $1
RETURNING *;

-- name: MarkVoided :one
UPDATE transactions
SET status = 'voided'
WHERE id = $1
RETURNING *;

-- name: MarkSettled :one
UPDATE transactions
SET status = CASE WHEN refunded_cents > 0 AND refunded_cents >= captured_cents
                    THEN 'refunded'
                  ELSE 'settled' END,
    settled_cents = captured_cents - refunded_cents,
    settled_at = $2,
    refund_deadline = $2::timestamptz + INTERVAL '90 days'
WHERE id = $1
RETURNING *;

-- name: CreateRefund :one
INSERT INTO refunds (payment_id, merchant_id, amount_cents, fee_refund_cents, net_cents, reason, created_at)
VALUES (
    sqlc.arg('payment_id'),
    sqlc.arg('merchant_id'),
    sqlc.arg('amount_cents'),
    sqlc.arg('fee_refund_cents'),
    sqlc.arg('net_cents'),
    sqlc.arg('reason'),
    sqlc.arg('created_at')::timestamptz
)
RETURNING *;

-- name: GetRefund :one
SELECT * FROM refunds WHERE id = $1;

-- name: ListRefundsByMerchant :many
SELECT * FROM refunds
WHERE merchant_id = $1
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;
