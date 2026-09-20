-- name: GetLatestPublishedVersion :one
SELECT id, store_id, version, published_at, published_by
FROM menu_versions
WHERE store_id = $1
ORDER BY version DESC
LIMIT 1;

-- name: GetPublishedVersionByNumber :one
SELECT id, store_id, version, published_at, published_by
FROM menu_versions
WHERE store_id = $1 AND version = $2;

-- name: GetVersionByID :one
SELECT id, store_id, version, published_at, published_by
FROM menu_versions WHERE id = $1;

-- name: InsertMenuVersion :one
INSERT INTO menu_versions (store_id, version, published_by)
VALUES ($1, $2, $3)
RETURNING id, store_id, version, published_at, published_by;

-- name: InsertMenuVersionItem :exec
INSERT INTO menu_version_items (version_id, dish_id, dish_name, position, price)
VALUES ($1, $2, $3, $4, $5);

-- name: ListVersionItems :many
SELECT id, version_id, dish_id, dish_name, position, price
FROM menu_version_items
WHERE version_id = $1
ORDER BY position;

-- name: InsertTempPrice :exec
INSERT INTO temp_prices (version_id, dish_id, price, starts_at, ends_at)
VALUES ($1, $2, $3, $4, $5);

-- name: DeleteTempPricesForVersion :exec
DELETE FROM temp_prices WHERE version_id = $1;

-- name: ListTempPricesForVersion :many
SELECT id, version_id, dish_id, price, starts_at, ends_at
FROM temp_prices
WHERE version_id = $1
ORDER BY dish_id, starts_at;

-- name: ActiveTempPricesAt :many
SELECT id, version_id, dish_id, price, starts_at, ends_at
FROM temp_prices
WHERE version_id = $1
  AND starts_at <= $2
  AND $2 < ends_at
ORDER BY dish_id, starts_at;
