-- name: CreateBatch :one
INSERT INTO import_batches
    (account_id, filename, total_rows, inserted_rows, duplicate_rows, status, created_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id, account_id, filename, total_rows, inserted_rows, duplicate_rows,
          status, created_by, created_at;

-- name: MarkBatch :exec
UPDATE import_batches SET status = $2 WHERE id = $1;

-- name: ListBatches :many
SELECT b.id, b.account_id, b.filename, b.total_rows, b.inserted_rows,
       b.duplicate_rows, b.status, b.created_by, b.created_at
FROM import_batches b
JOIN accounts a ON a.id = b.account_id
WHERE (cardinality(COALESCE(@org_ids, ARRAY[]::bigint[])) = 0 OR a.org_id = ANY(@org_ids))
ORDER BY b.id DESC
LIMIT $1 OFFSET $2;

-- name: ListDailySummaries :many
SELECT id, scope, account_id, cost_center_id, usage_date, currency,
       total_amount, record_count, updated_at
FROM daily_summaries
WHERE scope = $1
  AND (CASE WHEN $1 = 'account'
            THEN (cardinality(COALESCE(@account_ids, ARRAY[]::bigint[])) = 0 OR account_id = ANY(@account_ids))
            ELSE (cardinality(COALESCE(@cc_ids, ARRAY[]::bigint[])) = 0 OR cost_center_id = ANY(@cc_ids))
       END)
  AND usage_date BETWEEN $2 AND $3
ORDER BY usage_date, currency;

-- name: ListMonthlySummaries :many
SELECT id, scope, account_id, cost_center_id, month, currency,
       total_amount, record_count, updated_at
FROM monthly_summaries
WHERE scope = $1
  AND (CASE WHEN $1 = 'account'
            THEN (cardinality(COALESCE(@account_ids, ARRAY[]::bigint[])) = 0 OR account_id = ANY(@account_ids))
            ELSE (cardinality(COALESCE(@cc_ids, ARRAY[]::bigint[])) = 0 OR cost_center_id = ANY(@cc_ids))
       END)
  AND month BETWEEN $2 AND $3
ORDER BY month, currency;

-- name: ListBillingRecords :many
SELECT r.id, r.account_id, r.resource_id, r.service, r.usage_date,
       r.currency, r.amount, r.content_hash, r.batch_id, r.created_at
FROM billing_records r
WHERE (cardinality(COALESCE(@account_ids, ARRAY[]::bigint[])) = 0 OR r.account_id = ANY(@account_ids))
  AND (@currency_filter::boolean = FALSE OR r.currency = @currency)
  AND r.usage_date BETWEEN $1 AND $2
ORDER BY r.usage_date, r.resource_id
LIMIT $3 OFFSET $4;

-- name: CountBillingRecords :one
SELECT count(*) FROM billing_records r
WHERE (cardinality(COALESCE(@account_ids, ARRAY[]::bigint[])) = 0 OR r.account_id = ANY(@account_ids))
  AND (@currency_filter::boolean = FALSE OR r.currency = @currency)
  AND r.usage_date BETWEEN $1 AND $2;
