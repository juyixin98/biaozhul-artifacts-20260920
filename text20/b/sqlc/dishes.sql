-- name: CreateDish :one
INSERT INTO dishes (store_id, name, base_price)
VALUES ($1, $2, $3)
RETURNING id, store_id, name, base_price, is_active, created_at, updated_at;

-- name: GetDish :one
SELECT id, store_id, name, base_price, is_active, created_at, updated_at
FROM dishes WHERE id = $1 AND store_id = $2;

-- name: ListDishes :many
SELECT id, store_id, name, base_price, is_active, created_at, updated_at
FROM dishes WHERE store_id = $1 ORDER BY id;

-- name: UpsertDish :one
INSERT INTO dishes (store_id, name, base_price)
VALUES ($1, $2, $3)
ON CONFLICT (store_id, name) DO UPDATE
SET base_price = EXCLUDED.base_price,
    updated_at = now()
RETURNING id, store_id, name, base_price, is_active, created_at, updated_at;

-- name: SetDishPrice :execrows
UPDATE dishes SET base_price = $3, updated_at = now()
WHERE id = $1 AND store_id = $2;

-- name: SetDishActive :execrows
UPDATE dishes SET is_active = $3, updated_at = now()
WHERE id = $1 AND store_id = $2;

-- name: UpsertThreshold :one
INSERT INTO sellout_thresholds (store_id, dish_id, threshold)
VALUES ($1, $2, $3)
ON CONFLICT (store_id, dish_id) DO UPDATE SET threshold = EXCLUDED.threshold
RETURNING store_id, dish_id, threshold;

-- name: GetThreshold :one
SELECT store_id, dish_id, threshold FROM sellout_thresholds
WHERE store_id = $1 AND dish_id = $2;

-- name: ListThresholds :many
SELECT store_id, dish_id, threshold FROM sellout_thresholds
WHERE store_id = $1;
