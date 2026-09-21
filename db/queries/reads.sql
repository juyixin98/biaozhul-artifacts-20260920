-- name: ListPaymentsForMerchant :many
SELECT * FROM payments WHERE merchant_id = $1
ORDER BY created_at DESC LIMIT $2 OFFSET $3;

-- name: ListPaymentsAnyMerchant :many
SELECT * FROM payments
WHERE (sqlc.narg(merchant_id)::uuid IS NULL OR merchant_id = sqlc.narg(merchant_id)::uuid)
ORDER BY created_at DESC LIMIT sqlc.arg(row_limit) OFFSET sqlc.arg(row_offset);

-- name: GetPaymentAnyMerchant :one
SELECT * FROM payments WHERE id = $1;

-- name: ListRefundsForPayment :many
SELECT * FROM refunds WHERE payment_id = $1 AND merchant_id = $2 ORDER BY created_at;

-- name: ListRefundsForPaymentAnyMerchant :many
SELECT * FROM refunds WHERE payment_id = $1 ORDER BY created_at;

-- name: ListAllRefundsForMerchant :many
SELECT * FROM refunds WHERE merchant_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3;
