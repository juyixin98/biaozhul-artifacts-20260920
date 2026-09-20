-- name: UserByAPIKeyHash :one
SELECT * FROM users
WHERE api_key_hash = $1 AND active = true;

-- name: UserByID :one
SELECT * FROM users
WHERE id = $1;

-- name: ListUsers :many
SELECT * FROM users
WHERE active = true
ORDER BY username;
