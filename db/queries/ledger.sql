-- name: CreateAccount :one
INSERT INTO accounts (merchant_id, code, kind, description)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetAccount :one
SELECT * FROM accounts WHERE merchant_id IS NOT DISTINCT FROM $1 AND code = $2;

-- name: GetAccountByID :one
SELECT * FROM accounts WHERE id = $1;

-- name: ListAccountsByMerchant :many
SELECT * FROM accounts WHERE merchant_id = $1 ORDER BY code;

-- name: SumAccountEntries :one
SELECT COALESCE(SUM(amount_cents), 0)::BIGINT AS balance
FROM ledger_entries
WHERE account_id = $1;

-- name: SumAccountEntriesBefore :one
-- Reconciliation snapshots account balance from entries created on/before a cutoff.
SELECT COALESCE(SUM(amount_cents), 0)::BIGINT AS balance
FROM ledger_entries e
WHERE e.account_id = $1 AND e.created_at <= $2;

-- name: InsertLedgerEntry :exec
INSERT INTO ledger_entries (tx_type, ref_type, ref_id, account_id, amount_cents, created_at)
VALUES ($1, $2, $3, $4, $5, sqlc.arg('created_at')::timestamptz);

-- name: ListEntriesByRef :many
SELECT * FROM ledger_entries WHERE ref_type = $1 AND ref_id = $2 ORDER BY id;

-- name: ListEntriesByMerchant :many
SELECT e.* FROM ledger_entries e
JOIN accounts a ON a.id = e.account_id
WHERE a.merchant_id = $1
ORDER BY e.id
LIMIT $2 OFFSET $3;
