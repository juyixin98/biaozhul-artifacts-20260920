-- name: GetUserByAPIToken :one
SELECT * FROM users WHERE api_token = $1;

-- name: GetUserByID :one
SELECT * FROM users WHERE id = $1;

-- name: CreateUser :one
INSERT INTO users (id, email, display_name, role, api_token)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;
