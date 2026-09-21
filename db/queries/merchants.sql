-- name: CreateMerchant :one
INSERT INTO merchants (name, fee_bps, fee_fixed, api_key_hash, api_key_prefix)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetMerchant :one
SELECT * FROM merchants WHERE id = $1;

-- name: GetMerchantByAPIKeyHash :one
SELECT * FROM merchants WHERE api_key_hash = $1 AND status = 'active';

-- name: ListMerchants :many
SELECT * FROM merchants ORDER BY created_at DESC;

-- name: RotateMerchantAPIKey :one
UPDATE merchants SET api_key_hash = $2, api_key_prefix = $3
WHERE id = $1 RETURNING *;

-- name: SetMerchantStatus :one
UPDATE merchants SET status = $2 WHERE id = $1 RETURNING *;

-- name: CreateUser :one
INSERT INTO users (email, password_hash, role, merchant_id)
VALUES ($1, $2, $3, $4) RETURNING *;

-- name: GetUserByEmail :one
SELECT * FROM users WHERE email = $1;

-- name: GetUser :one
SELECT * FROM users WHERE id = $1;

-- name: ListUsersByMerchant :many
SELECT * FROM users WHERE merchant_id = $1 ORDER BY created_at;

-- name: ListUsers :many
SELECT * FROM users ORDER BY created_at;

-- name: CreateLedgerAccount :one
INSERT INTO ledger_accounts (code, name, kind)
VALUES ($1, $2, $3) RETURNING *;

-- name: GetLedgerAccountByCode :one
SELECT * FROM ledger_accounts WHERE code = $1;

-- name: PayableAccountForUpdate :one
SELECT * FROM ledger_accounts WHERE code = 'PAYABLE:' || $1::text FOR UPDATE;

-- name: GlobalAccountForUpdate :one
SELECT * FROM ledger_accounts WHERE code = $1 FOR UPDATE;

-- name: InsertLedgerEntry :exec
INSERT INTO ledger_entries (account_id, ref_type, ref_id, amount)
VALUES ($1, $2, $3, $4);

-- name: AccountBalance :one
SELECT COALESCE(SUM(amount), 0)::bigint AS balance FROM ledger_entries WHERE account_id = $1;

-- name: EntriesForRef :many
SELECT * FROM ledger_entries WHERE ref_type = $1 AND ref_id = $2 ORDER BY id;
