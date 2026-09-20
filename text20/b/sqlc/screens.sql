-- name: CreateScreen :one
INSERT INTO screens (store_id, name, token_hash)
VALUES ($1, $2, $3)
RETURNING id, store_id, name, token_hash, last_seen_at, created_at;

-- name: GetScreenByTokenHash :one
SELECT id, store_id, name, token_hash, last_seen_at, created_at
FROM screens WHERE token_hash = $1;

-- name: TouchScreen :exec
UPDATE screens SET last_seen_at = now() WHERE id = $1;

-- name: ListScreensByStore :many
SELECT id, store_id, name, token_hash, last_seen_at, created_at
FROM screens WHERE store_id = $1 ORDER BY id;
