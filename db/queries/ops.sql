-- name: UpsertGatewayStatement :one
INSERT INTO gateway_statements (merchant_id, ref_id, kind, amount_cents, stmt_date)
VALUES ($1, $2, $3, $4, sqlc.arg('stmt_date')::date)
ON CONFLICT (merchant_id, ref_id, kind)
DO UPDATE SET amount_cents = EXCLUDED.amount_cents, stmt_date = EXCLUDED.stmt_date
RETURNING *;

-- name: ListGatewayStatements :many
SELECT * FROM gateway_statements
WHERE merchant_id = $1 AND stmt_date = sqlc.arg('stmt_date')::date
ORDER BY id;

-- name: ListGatewayStatementsBefore :many
SELECT * FROM gateway_statements
WHERE merchant_id = $1 AND stmt_date < sqlc.arg('stmt_date')::date
ORDER BY id;

-- name: ListAllGatewayStatements :many
SELECT * FROM gateway_statements
WHERE merchant_id = $1
ORDER BY id;

-- name: SumGatewayStatements :one
SELECT kind, COALESCE(SUM(amount_cents),0)::BIGINT AS total, COUNT(*)::BIGINT AS n
FROM gateway_statements
WHERE merchant_id = $1 AND stmt_date <= $2
GROUP BY kind;

-- name: ListPaymentsCreatedBefore :many
SELECT * FROM transactions
WHERE merchant_id = $1 AND created_at < $2
ORDER BY created_at;

-- name: ListRefundsCreatedBefore :many
SELECT r.* FROM refunds r
JOIN transactions t ON t.id = r.payment_id
WHERE r.merchant_id = $1 AND r.created_at < $2
ORDER BY r.created_at;

-- name: CreateReconRun :one
INSERT INTO reconciliation_runs (merchant_id, run_date, status)
VALUES ($1, sqlc.arg('run_date')::date, 'running')
RETURNING *;

-- name: GetReconRun :one
SELECT * FROM reconciliation_runs WHERE merchant_id = $1 AND run_date = sqlc.arg('run_date')::date;

-- name: GetReconRunForUpdate :one
SELECT * FROM reconciliation_runs WHERE merchant_id = $1 AND run_date = sqlc.arg('run_date')::date FOR UPDATE;

-- name: CompleteReconRun :one
UPDATE reconciliation_runs
SET status = 'done',
    batch_id = $2,
    captured_cents = $3,
    refunded_cents = $4,
    fees_cents = $5,
    settled_cents = $6,
    gateway_cash_cents = $7,
    payable_cents = $8,
    expected_cash_cents = $9,
    diff_cents = $10,
    discrepancies_count = $11,
    completed_at = $12
WHERE id = $1
RETURNING *;

-- name: DeleteDiscrepanciesForRun :exec
DELETE FROM discrepancies WHERE run_id = $1;

-- name: InsertDiscrepancy :one
INSERT INTO discrepancies
    (run_id, kind, tx_id, expected_cents, actual_cents, diff_cents, detail)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: ListDiscrepancies :many
SELECT * FROM discrepancies WHERE run_id = $1 ORDER BY id;

-- name: ListReconRuns :many
SELECT * FROM reconciliation_runs WHERE merchant_id = $1 ORDER BY run_date DESC;

-- name: InsertAuditEvent :exec
INSERT INTO audit_events (actor_key_id, actor_role, merchant_id, action, target_type, target_id, metadata)
VALUES ($1, $2, $3, $4, $5, $6, $7);

-- name: ListAuditEvents :many
SELECT * FROM audit_events
WHERE (sqlc.narg('merchant_id')::uuid IS NULL OR merchant_id = sqlc.narg('merchant_id'))
ORDER BY id DESC
LIMIT $1 OFFSET $2;

-- name: ListMerchantIDs :many
SELECT id FROM merchants WHERE active = TRUE ORDER BY id;
