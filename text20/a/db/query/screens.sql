-- name: CreateScreen :one
INSERT INTO screens (store_id, name, token_hash)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetScreenByTokenHash :one
SELECT * FROM screens WHERE token_hash = $1;

-- name: GetScreen :one
SELECT * FROM screens WHERE id = $1 AND store_id = $2;

-- name: ListScreens :many
SELECT * FROM screens WHERE store_id = $1 ORDER BY id;

-- The heartbeat instant is supplied by the application clock (nowFn) rather
-- than SQL now(), so liveness evaluation and the stored timestamp share one
-- clock (important under a pinned test clock).
-- name: HeartbeatScreen :exec
UPDATE screens SET last_heartbeat = $2 WHERE id = $1;
