-- name: CreateComposition :one
INSERT INTO compositions (id, project_id, name, canvas_width, canvas_height, frame_count, created_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: GetComposition :one
SELECT * FROM compositions WHERE id = $1;

-- name: ListCompositions :many
SELECT * FROM compositions WHERE project_id = $1 ORDER BY created_at DESC;

-- name: SetCompositionCurrentVersion :exec
UPDATE compositions SET current_version_id = $2 WHERE id = $1;

-- name: NextVersionNumber :one
SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number
FROM composition_versions WHERE composition_id = $1;

-- name: CreateCompositionVersion :one
INSERT INTO composition_versions (id, composition_id, version_number, spec_json, spec_sha256, created_by)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: AddVersionResource :exec
INSERT INTO composition_version_resources (version_id, layer_id, asset_id, asset_sha256)
VALUES ($1, $2, $3, $4);

-- name: GetVersion :one
SELECT * FROM composition_versions WHERE id = $1;

-- name: ListVersionResources :many
SELECT * FROM composition_version_resources WHERE version_id = $1;

-- name: ListVersions :many
SELECT * FROM composition_versions WHERE composition_id = $1 ORDER BY version_number DESC;
