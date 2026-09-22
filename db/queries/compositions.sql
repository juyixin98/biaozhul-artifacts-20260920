-- name: CreateComposition :one
INSERT INTO compositions (project_id, name, width, height, created_by)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetComposition :one
SELECT * FROM compositions WHERE id = $1;

-- name: GetCompositionForUpdate :one
SELECT * FROM compositions WHERE id = $1 FOR UPDATE;

-- name: ListCompositions :many
SELECT * FROM compositions WHERE project_id = $1 ORDER BY name;
