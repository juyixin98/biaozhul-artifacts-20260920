-- name: UpsertAsset :one
INSERT INTO assets (project_id, rel_path, width, height, size_bytes, sha256, uploaded_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (project_id, rel_path) DO UPDATE
SET width       = EXCLUDED.width,
    height      = EXCLUDED.height,
    size_bytes  = EXCLUDED.size_bytes,
    sha256      = EXCLUDED.sha256,
    uploaded_by = EXCLUDED.uploaded_by,
    created_at  = now()
RETURNING *;

-- name: GetAssetByPath :one
SELECT * FROM assets
WHERE project_id = $1 AND rel_path = $2;

-- name: GetAssetByID :one
SELECT * FROM assets WHERE id = $1;

-- name: ListAssets :many
SELECT * FROM assets WHERE project_id = $1 ORDER BY rel_path;
