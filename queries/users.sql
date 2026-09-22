-- name: CreateUser :one
INSERT INTO users (username, role, api_token)
VALUES ($1, $2, $3)
RETURNING id, username, role, api_token, created_at;

-- name: GetUserByToken :one
SELECT id, username, role, api_token, created_at FROM users WHERE api_token = $1;

-- name: ListGrantedOrgIDs :many
SELECT org_id FROM user_org_grants WHERE user_id = $1 ORDER BY org_id;

-- name: GrantOrg :exec
INSERT INTO user_org_grants (user_id, org_id) VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: ListUsers :many
SELECT id, username, role, api_token, created_at FROM users ORDER BY id;
