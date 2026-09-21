-- name: CreateScreen :one
INSERT INTO screens (store_id, name, token_hash) VALUES ($1, $2, $3) RETURNING id, store_id, name, token_hash, created_at, last_heartbeat_at;

-- name: GetScreenByTokenHash :one
SELECT id, store_id, name, token_hash, created_at, last_heartbeat_at FROM screens WHERE token_hash = $1;

-- name: ListScreens :many
SELECT id, store_id, name, created_at, last_heartbeat_at,
       COALESCE(last_heartbeat_at > now() - interval '90 seconds', false) AS online
FROM screens WHERE store_id = $1 ORDER BY created_at;

-- name: Heartbeat :exec
UPDATE screens SET last_heartbeat_at = now() WHERE id = $1;
