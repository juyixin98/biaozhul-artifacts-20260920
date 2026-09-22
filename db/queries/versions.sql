-- name: NextVersionNo :one
SELECT coalesce(max(version_no), 0) + 1::bigint AS next_no
FROM versions
WHERE composition_id = $1;

-- name: CreateVersion :one
INSERT INTO versions (composition_id, version_no, manifest, manifest_sha256, created_by)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: CreateVersionResource :exec
INSERT INTO version_resources (version_id, layer_id, asset_id, sha256)
VALUES ($1, $2, $3, $4);

-- name: GetVersion :one
SELECT * FROM versions WHERE id = $1;

-- name: ListVersionResources :many
SELECT * FROM version_resources WHERE version_id = $1 ORDER BY layer_id;

-- name: ListVersions :many
SELECT * FROM versions
WHERE composition_id = $1
ORDER BY version_no DESC;
