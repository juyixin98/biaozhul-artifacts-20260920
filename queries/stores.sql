-- name: CreateStore :one
INSERT INTO stores (name, timezone) VALUES ($1, $2) RETURNING id, name, timezone, current_version, created_at;

-- name: GetStore :one
SELECT id, name, timezone, current_version, created_at FROM stores WHERE id = $1;

-- name: EnsureDraft :exec
INSERT INTO drafts (store_id) VALUES ($1) ON CONFLICT (store_id) DO NOTHING;

-- name: ListVersions :many
SELECT id, store_id, version, published_at FROM menu_versions WHERE store_id = $1 ORDER BY version DESC;
