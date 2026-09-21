-- name: GetUser :one
SELECT id, username, role, created_at FROM users WHERE id = $1;

-- name: ListScopes :many
SELECT user_id, category FROM moderator_scopes WHERE user_id = $1;
