-- name: CreateMerchant :one
INSERT INTO merchants (name, fee_bps, fee_fixed_cents)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetMerchant :one
SELECT * FROM merchants WHERE id = $1;

-- name: ListMerchants :many
SELECT * FROM merchants ORDER BY created_at;

-- name: UpdateMerchant :one
UPDATE merchants
SET name = $2, fee_bps = $3, fee_fixed_cents = $4, active = $5
WHERE id = $1
RETURNING *;

-- name: CreateAPIKey :one
INSERT INTO api_keys (key_hash, key_prefix, merchant_id, role, label)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetAPIKeyByHash :one
SELECT * FROM api_keys WHERE key_hash = $1 AND active = TRUE;

-- name: ListAPIKeys :many
SELECT * FROM api_keys
WHERE (sqlc.narg('merchant_id')::uuid IS NULL OR merchant_id = sqlc.narg('merchant_id'))
ORDER BY created_at;
