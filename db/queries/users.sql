-- name: CreateUser :one
INSERT INTO users (username, role, api_key_hash)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetUserByAPIKeyHash :one
SELECT * FROM users WHERE api_key_hash = $1;

-- name: GetUserByID :one
SELECT * FROM users WHERE id = $1;

-- name: GetUserByName :one
SELECT * FROM users WHERE username = $1;

-- name: ListUsers :many
SELECT * FROM users ORDER BY username;
