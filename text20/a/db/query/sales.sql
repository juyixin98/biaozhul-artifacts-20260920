-- Idempotent insert. A second submission of the same (store_id, event_id)
-- inserts nothing; the handler then compares the stored row to the payload to
-- tell a harmless duplicate from a conflicting re-use of the same ID.
-- name: InsertSalesEvent :one
INSERT INTO sales_events (store_id, event_id, sku, qty, occurred_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (store_id, event_id) DO NOTHING
RETURNING *;

-- name: GetSalesEvent :one
SELECT * FROM sales_events
WHERE store_id = $1 AND event_id = $2;

-- Atomic counter. ON CONFLICT takes a row lock on the target daily_sales row,
-- so concurrent events for the same (store, day, sku) serialize and no
-- increment is ever lost.
-- name: IncrementDailySales :one
INSERT INTO daily_sales (store_id, sales_day, sku, qty)
VALUES ($1, $2, $3, $4)
ON CONFLICT (store_id, sales_day, sku)
DO UPDATE SET qty = daily_sales.qty + EXCLUDED.qty
RETURNING *;

-- name: GetDishLimit :one
SELECT daily_limit FROM dishes
WHERE store_id = $1 AND sku = $2 AND active = TRUE;

-- name: MarkSoldOut :exec
INSERT INTO sold_outs (store_id, sku, sales_day)
VALUES ($1, $2, $3)
ON CONFLICT DO NOTHING;

-- name: ListSoldOutsForDay :many
SELECT sku, triggered_at FROM sold_outs
WHERE store_id = $1 AND sales_day = $2;

-- name: GetDailySales :many
SELECT sku, qty FROM daily_sales
WHERE store_id = $1 AND sales_day = $2;
