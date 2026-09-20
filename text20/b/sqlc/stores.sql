-- name: CreateStore :one
INSERT INTO stores (name, timezone)
VALUES ($1, $2)
RETURNING id, name, timezone, created_at;

-- name: GetStore :one
SELECT id, name, timezone, created_at FROM stores WHERE id = $1;

-- name: ListStores :many
SELECT id, name, timezone, created_at FROM stores ORDER BY id;

-- name: LockStore :exec
SELECT id FROM stores WHERE id = $1 FOR UPDATE;
