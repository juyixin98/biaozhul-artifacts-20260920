-- name: GetAPIKeyByHash :one
SELECT * FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL;

-- name: GetOrganization :one
SELECT * FROM organizations WHERE id = $1;

-- name: GetOrganizationByName :one
SELECT * FROM organizations WHERE name = $1;

-- name: ListOrganizations :many
SELECT * FROM organizations ORDER BY id;

-- name: CreateOrganization :one
INSERT INTO organizations (name, timezone) VALUES ($1, $2) RETURNING *;

-- name: GetUser :one
SELECT * FROM users WHERE id = $1;

-- name: GetUserByName :one
SELECT * FROM users WHERE org_id = $1 AND username = $2;

-- name: CreateUser :one
INSERT INTO users (org_id, username, display_name) VALUES ($1, $2, $3) RETURNING *;

-- name: CreateAPIKey :one
INSERT INTO api_keys (key_hash, key_prefix, org_id, name, role)
VALUES ($1, $2, $3, $4, $5) RETURNING *;

-- name: GetSource :one
SELECT * FROM sources WHERE id = $1;

-- name: GetSourceByName :one
SELECT * FROM sources WHERE org_id = $1 AND source_name = $2;

-- name: ListSources :many
SELECT * FROM sources WHERE org_id = $1 ORDER BY source_name;

-- name: CreateSource :one
INSERT INTO sources (org_id, source_name, db_type) VALUES ($1, $2, $3) RETURNING *;

-- name: GetOrCreateSource :one
INSERT INTO sources (org_id, source_name, db_type) VALUES ($1, $2, 'postgresql')
ON CONFLICT (org_id, source_name) DO UPDATE SET source_name = EXCLUDED.source_name
RETURNING *;
