-- name: LockStoreForPublish :one
SELECT id, name, timezone, menu_version
FROM stores
WHERE id = $1
FOR UPDATE;

-- name: CreateMenuVersion :one
INSERT INTO menu_versions (store_id, version, note)
VALUES ($1, $2, $3)
RETURNING *;

-- name: BumpStoreVersion :exec
UPDATE stores SET menu_version = $2 WHERE id = $1;

-- name: GetLatestVersion :one
SELECT * FROM menu_versions
WHERE store_id = $1
ORDER BY version DESC
LIMIT 1;

-- name: GetVersionByNumber :one
SELECT * FROM menu_versions
WHERE store_id = $1 AND version = $2;

-- name: ListVersions :many
SELECT * FROM menu_versions
WHERE store_id = $1
ORDER BY version DESC;

-- name: CopyVersionItems :copyfrom
INSERT INTO menu_version_items (version_id, sku, name, base_price, display_order)
VALUES ($1, $2, $3, $4, $5);

-- name: ListVersionItems :many
SELECT * FROM menu_version_items
WHERE version_id = $1
ORDER BY display_order, id;

-- name: CountVersionItems :one
SELECT count(*) FROM menu_version_items WHERE version_id = $1;
