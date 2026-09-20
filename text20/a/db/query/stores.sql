-- name: CreateStore :one
INSERT INTO stores (name, timezone)
VALUES ($1, $2)
RETURNING *;

-- name: GetStore :one
SELECT * FROM stores WHERE id = $1;

-- name: ListStores :many
SELECT * FROM stores ORDER BY id;

-- name: GetStoreVersion :one
SELECT menu_version FROM stores WHERE id = $1;
