-- name: InsertSalesEvent :one
-- Inserts the event or returns nothing when the id already exists.
-- The unique PK serializes concurrent duplicates: exactly one writer
-- gets a row back and only that writer increments the aggregate.
INSERT INTO sales_events (id, store_id, item_key, quantity, occurred_at)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (id) DO NOTHING
RETURNING id;

-- name: GetSalesEvent :one
SELECT id, store_id, item_key, quantity, occurred_at, received_at FROM sales_events WHERE id = $1;

-- name: AddDailySales :exec
INSERT INTO daily_sales (store_id, item_key, day, quantity)
VALUES ($1, $2, $3, $4)
ON CONFLICT (store_id, item_key, day)
DO UPDATE SET quantity = daily_sales.quantity + EXCLUDED.quantity;

-- name: ListDailySales :many
SELECT store_id, item_key, day, quantity FROM daily_sales WHERE store_id = $1 AND day = $2 ORDER BY item_key;
