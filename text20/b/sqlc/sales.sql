-- name: InsertSalesEvent :one
INSERT INTO sales_events (store_id, event_id, dish_id, quantity, occurred_at)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, store_id, event_id, dish_id, quantity, occurred_at, received_at;

-- name: GetSalesEvent :one
SELECT id, store_id, event_id, dish_id, quantity, occurred_at, received_at
FROM sales_events WHERE store_id = $1 AND event_id = $2;

-- name: UpsertDailySales :one
INSERT INTO daily_sales (store_id, sales_day, dish_id, quantity)
VALUES ($1, $2, $3, $4)
ON CONFLICT (store_id, sales_day, dish_id)
DO UPDATE SET quantity = daily_sales.quantity + EXCLUDED.quantity
RETURNING id, store_id, sales_day, dish_id, quantity, sold_out;

-- name: MarkSoldOut :exec
UPDATE daily_sales SET sold_out = true
WHERE store_id = $1 AND sales_day = $2 AND dish_id = $3;

-- name: GetDailySales :one
SELECT id, store_id, sales_day, dish_id, quantity, sold_out
FROM daily_sales WHERE store_id = $1 AND sales_day = $2 AND dish_id = $3;

-- name: ListSoldOutForDay :many
SELECT dish_id FROM daily_sales
WHERE store_id = $1 AND sales_day = $2 AND sold_out = true;

-- name: ListDailySalesForDay :many
SELECT id, store_id, sales_day, dish_id, quantity, sold_out
FROM daily_sales
WHERE store_id = $1 AND sales_day = $2
ORDER BY dish_id;

-- name: CountSoldOutByDay :one
SELECT count(*) FROM daily_sales
WHERE store_id = $1 AND sales_day = $2 AND sold_out = true;
