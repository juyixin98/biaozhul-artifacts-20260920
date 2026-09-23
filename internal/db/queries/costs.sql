-- name: InsertCosts :exec
INSERT INTO costs (import_id, account_id, resource_id, service, cost_date, currency, amount)
SELECT $1::uuid,
       UNNEST($2::text[])::uuid, UNNEST($3::text[])::uuid, UNNEST($4::text[]),
       UNNEST($5::text[])::date, UNNEST($6::text[])::char(3),
       UNNEST($7::text[])::numeric;

-- name: GetExistingCostsForKeys :many
SELECT c.account_id, c.resource_id, c.cost_date, c.service, c.currency, c.amount
FROM costs c
WHERE c.cost_date BETWEEN $1 AND $2
  AND c.account_id = ANY($3::uuid[]);

-- name: MinCostDate :one
-- Sentinel 0001-01-01 (Go time.Time zero) when the table is empty.
SELECT coalesce(min(cost_date), '0001-01-01'::date)::date AS min_date FROM costs;

-- name: MaxCostDate :one
SELECT coalesce(max(cost_date), '0001-01-01'::date)::date AS max_date FROM costs;

-- name: CountCostsScoped :one
SELECT count(*)
FROM costs c
JOIN accounts a ON a.id = c.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR c.account_id = @account_id)
  AND (sqlc.narg('date_eq')::date IS NULL OR c.cost_date = @date_eq)
  AND (sqlc.narg('date_from')::date IS NULL OR c.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR c.cost_date <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR c.currency = @currency);

-- name: ListCostsScoped :many
SELECT c.id, c.import_id, c.account_id, c.resource_id, c.service,
       c.cost_date, c.currency, c.amount, c.created_at
FROM costs c
JOIN accounts a ON a.id = c.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR c.account_id = @account_id)
  AND (sqlc.narg('date_eq')::date IS NULL OR c.cost_date = @date_eq)
  AND (sqlc.narg('date_from')::date IS NULL OR c.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR c.cost_date <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR c.currency = @currency)
ORDER BY c.cost_date, c.account_id, c.resource_id
LIMIT $2 OFFSET $3;

-- name: StreamCostsScoped :many
SELECT c.account_id, c.resource_id, c.service, c.cost_date, c.currency, c.amount
FROM costs c
JOIN accounts a ON a.id = c.account_id
WHERE a.org_id = ANY($1::uuid[])
  AND (sqlc.narg('account_id')::uuid IS NULL OR c.account_id = @account_id)
  AND (sqlc.narg('date_eq')::date IS NULL OR c.cost_date = @date_eq)
  AND (sqlc.narg('date_from')::date IS NULL OR c.cost_date >= @date_from)
  AND (sqlc.narg('date_to')::date IS NULL OR c.cost_date <= @date_to)
  AND (sqlc.narg('currency')::char(3) IS NULL OR c.currency = @currency)
ORDER BY c.cost_date, c.account_id, c.resource_id;

-- Daily totals per account/currency in a window. Zero-spend days are absent
-- and are filled in Go (baseline must include zero days).
-- name: DailyAccountTotalsInRange :many
SELECT account_id, currency, cost_date, total, row_count
FROM daily_account_summary
WHERE cost_date BETWEEN $1 AND $2
ORDER BY account_id, currency, cost_date;
