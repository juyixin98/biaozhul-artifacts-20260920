-- name: GetUserByTokenHash :one
SELECT u.id, u.email, u.display_name
FROM api_tokens t
JOIN users u ON u.id = t.user_id
WHERE t.token_hash = $1;

-- name: GetMembership :one
SELECT org_id, user_id, role FROM memberships
WHERE org_id = $1 AND user_id = $2;

-- name: GetOrg :one
SELECT id, slug, name, timezone FROM organizations WHERE slug = $1;

-- name: GetOrgByID :one
SELECT id, slug, name, timezone FROM organizations WHERE id = $1;

-- name: ListOrgs :many
SELECT id, slug, name, timezone FROM organizations ORDER BY id;

-- name: CreateOrg :one
INSERT INTO organizations (slug, name, timezone)
VALUES ($1, $2, $3)
RETURNING id, slug, name, timezone, created_at;

-- name: CreateUser :one
INSERT INTO users (email, display_name)
VALUES ($1, $2)
ON CONFLICT (email) DO UPDATE SET display_name = EXCLUDED.display_name
RETURNING id, email, display_name, created_at;

-- name: UpsertMembership :exec
INSERT INTO memberships (org_id, user_id, role)
VALUES ($1, $2, $3)
ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role;

-- name: InsertToken :exec
INSERT INTO api_tokens (user_id, token_hash, label)
VALUES ($1, $2, $3)
ON CONFLICT (token_hash) DO NOTHING;

-- name: CreateSource :one
INSERT INTO sources (org_id, source_key, name)
VALUES ($1, $2, $3)
ON CONFLICT (org_id, source_key)
DO UPDATE SET name = EXCLUDED.name
RETURNING id, org_id, source_key, name, created_at;

-- name: GetSource :one
SELECT id, org_id, source_key, name, created_at
FROM sources WHERE org_id = $1 AND source_key = $2;

-- name: ListSources :many
SELECT id, org_id, source_key, name, created_at
FROM sources WHERE org_id = $1 ORDER BY id;

-- name: UpsertExportPolicy :one
INSERT INTO export_policies (org_id, masked_fields)
VALUES ($1, $2)
ON CONFLICT (org_id) DO UPDATE
  SET masked_fields = EXCLUDED.masked_fields, updated_at = now()
RETURNING org_id, masked_fields, updated_at;

-- name: GetExportPolicy :one
SELECT org_id, masked_fields, updated_at FROM export_policies WHERE org_id = $1;
