-- Stores ---------------------------------------------------------------------

-- name: CreateStore :one
INSERT INTO stores (name, timezone) VALUES ($1, $2) RETURNING *;

-- name: GetStore :one
SELECT * FROM stores WHERE id = $1;

-- name: ListStores :many
SELECT * FROM stores ORDER BY created_at, id;

-- name: LockStore :one
SELECT id, timezone, current_version FROM stores WHERE id = $1 FOR UPDATE;

-- name: SetStoreVersion :exec
UPDATE stores SET current_version = $2 WHERE id = $1;

-- Draft items ----------------------------------------------------------------

-- name: CreateItem :one
INSERT INTO items (store_id, name, description, category, price_cents, sold_out_threshold, position)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: GetItem :one
SELECT * FROM items WHERE id = $1 AND store_id = $2;

-- name: UpdateItem :one
UPDATE items
SET name = $3, description = $4, category = $5, price_cents = $6,
    sold_out_threshold = $7, position = $8, updated_at = now()
WHERE id = $1 AND store_id = $2
RETURNING *;

-- name: DeleteItem :exec
DELETE FROM items WHERE id = $1 AND store_id = $2;

-- name: ListDraftItems :many
SELECT * FROM items WHERE store_id = $1 AND archived = FALSE
ORDER BY position, created_at, id;

-- name: CountDraftItems :one
SELECT count(*)::int FROM items WHERE store_id = $1 AND archived = FALSE;

-- Publishing -----------------------------------------------------------------

-- name: CreateMenuVersion :one
INSERT INTO menu_versions (store_id, version) VALUES ($1, $2) RETURNING *;

-- name: SnapshotDraftItems :exec
INSERT INTO menu_version_items (version_id, item_id, name, description, category, price_cents, sold_out_threshold, position)
SELECT $1, id, name, description, category, price_cents, sold_out_threshold, position
FROM items
WHERE store_id = $2 AND archived = FALSE
ORDER BY position, created_at, id;

-- name: ListVersions :many
SELECT * FROM menu_versions WHERE store_id = $1 ORDER BY version DESC;

-- name: GetVersion :one
SELECT * FROM menu_versions WHERE store_id = $1 AND version = $2;

-- name: ListVersionItems :many
SELECT * FROM menu_version_items WHERE version_id = $1 ORDER BY position, item_id;

-- Temporary prices -----------------------------------------------------------

-- name: CreateTempPrice :one
INSERT INTO temp_prices (store_id, item_id, price_cents, "window")
VALUES (sqlc.arg(store_id), sqlc.arg(item_id), sqlc.arg(price_cents),
        tstzrange(sqlc.arg(starts_at)::timestamptz, sqlc.arg(ends_at)::timestamptz, '[)'))
RETURNING id, store_id, item_id, price_cents,
          lower("window")::timestamptz AS starts_at, upper("window")::timestamptz AS ends_at, created_at;

-- name: ListTempPrices :many
SELECT id, store_id, item_id, price_cents,
       lower("window")::timestamptz AS starts_at, upper("window")::timestamptz AS ends_at, created_at
FROM temp_prices
WHERE store_id = $1
ORDER BY starts_at, id;

-- name: DeleteTempPrice :exec
DELETE FROM temp_prices WHERE id = $1 AND store_id = $2;

-- name: ListActiveTempPrices :many
SELECT item_id, price_cents,
       lower("window")::timestamptz AS starts_at, upper("window")::timestamptz AS ends_at
FROM temp_prices
WHERE store_id = sqlc.arg(store_id) AND "window" @> sqlc.arg(now)::timestamptz;

-- Sales events ---------------------------------------------------------------

-- name: InsertSalesEvent :one
INSERT INTO sales_events (store_id, event_id, item_id, quantity, occurred_at, sale_date, content_hash)
VALUES (sqlc.arg(store_id), sqlc.arg(event_id), sqlc.arg(item_id), sqlc.arg(quantity),
        sqlc.arg(occurred_at), sqlc.arg(sale_date)::date, sqlc.arg(content_hash))
ON CONFLICT (store_id, event_id) DO NOTHING
RETURNING event_id;

-- name: GetSalesEvent :one
SELECT * FROM sales_events WHERE store_id = $1 AND event_id = $2;

-- name: AddDailySales :one
INSERT INTO daily_sales (store_id, item_id, sale_date, quantity)
VALUES (sqlc.arg(store_id), sqlc.arg(item_id), sqlc.arg(sale_date)::date, sqlc.arg(quantity))
ON CONFLICT (store_id, item_id, sale_date)
DO UPDATE SET quantity = daily_sales.quantity + EXCLUDED.quantity
RETURNING quantity;

-- name: GetDailySales :many
SELECT item_id, quantity FROM daily_sales
WHERE store_id = sqlc.arg(store_id) AND sale_date = sqlc.arg(sale_date)::date
ORDER BY item_id;

-- Screens --------------------------------------------------------------------

-- name: CreateScreen :one
INSERT INTO screens (store_id, name, token_hash)
VALUES ($1, $2, $3)
RETURNING id, store_id, name, last_heartbeat_at, created_at;

-- name: GetScreenByTokenHash :one
SELECT * FROM screens WHERE token_hash = $1;

-- name: ListScreens :many
SELECT * FROM screens WHERE store_id = $1 ORDER BY created_at, id;

-- name: TouchHeartbeat :exec
UPDATE screens SET last_heartbeat_at = now() WHERE id = $1;

-- Screen menu (single consistent snapshot) ------------------------------------

-- name: GetCurrentVersionItems :many
SELECT mvi.item_id, mvi.name, mvi.description, mvi.category, mvi.price_cents,
       mvi.sold_out_threshold, mvi.position
FROM menu_versions mv
JOIN menu_version_items mvi ON mvi.version_id = mv.id
WHERE mv.store_id = $1 AND mv.version = $2
ORDER BY mvi.position, mvi.item_id;
