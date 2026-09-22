-- name: CreateCommunity :one
INSERT INTO communities (name) VALUES ($1) RETURNING *;

-- name: GetCommunity :one
SELECT * FROM communities WHERE id = $1;

-- name: CreateUser :one
INSERT INTO users (community_id, username, role, token)
VALUES ($1, $2, $3, $4) RETURNING *;

-- name: GetUserByToken :one
SELECT * FROM users WHERE token = $1;

-- name: GetUser :one
SELECT * FROM users WHERE id = $1 AND community_id = $2;

-- name: ListUsers :many
SELECT * FROM users WHERE community_id = $1 ORDER BY id;
