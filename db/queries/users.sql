-- name: CreateUser :one
INSERT INTO users (email, display_name, role, password_hash)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetUserByEmail :one
SELECT * FROM users WHERE email = $1;

-- name: GetUserByID :one
SELECT * FROM users WHERE id = $1;

-- name: ListUsers :many
SELECT * FROM users ORDER BY id;

-- name: CreateCommunity :one
INSERT INTO communities (name, created_by) VALUES ($1, $2)
RETURNING *;

-- name: GetCommunity :one
SELECT * FROM communities WHERE id = $1;

-- name: ListCommunities :many
SELECT * FROM communities ORDER BY id;

-- name: AddReviewer :exec
INSERT INTO community_reviewers (community_id, user_id) VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: IsReviewer :one
SELECT EXISTS(
    SELECT 1 FROM community_reviewers
    WHERE community_id = $1 AND user_id = $2
) AS is_reviewer;
