-- name: CreateDish :one
INSERT INTO dishes (store_id, sku, name, base_price, active, daily_limit, sort_order)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: UpdateDish :one
UPDATE dishes
SET name = $2,
    base_price = $3,
    active = $4,
    daily_limit = $5,
    sort_order = $6,
    updated_at = now()
WHERE id = $1
RETURNING *;

-- name: DeleteDish :exec
DELETE FROM dishes WHERE id = $1 AND store_id = $2;

-- name: GetDish :one
SELECT * FROM dishes WHERE id = $1 AND store_id = $2;

-- name: ListDishes :many
SELECT * FROM dishes WHERE store_id = $1 ORDER BY sort_order, id;

-- Only active dishes are snapshotted into a publish.
-- name: ListActiveDishesForPublish :many
SELECT sku, name, base_price, sort_order
FROM dishes
WHERE store_id = $1 AND active = TRUE
ORDER BY sort_order, id;

-- Thresholds are catalog configuration; sold-out checks read the current
-- per-SKU limit independently of which menu version is live.
-- name: ListDishLimits :many
SELECT sku, daily_limit
FROM dishes
WHERE store_id = $1 AND active = TRUE;
