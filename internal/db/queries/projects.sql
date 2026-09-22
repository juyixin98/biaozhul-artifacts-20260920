-- name: CreateProject :one
INSERT INTO projects (id, name, created_by)
VALUES ($1, $2, $3)
RETURNING *;

-- name: GetProject :one
SELECT * FROM projects WHERE id = $1;

-- name: AddProjectMember :exec
INSERT INTO project_members (project_id, user_id, role)
VALUES ($1, $2, $3)
ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role;

-- name: GetProjectMembership :one
SELECT * FROM project_members WHERE project_id = $1 AND user_id = $2;

-- name: ListMemberships :many
SELECT project_id, role, added_at FROM project_members WHERE user_id = $1 ORDER BY added_at;

-- name: CreateAsset :one
INSERT INTO assets (id, project_id, filename, content_type, width, height, size_bytes,
                    sha256, storage_path, uploaded_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING *;

-- name: GetAssetByProjectHash :one
SELECT * FROM assets WHERE project_id = $1 AND sha256 = $2;

-- name: GetAssetByID :one
SELECT * FROM assets WHERE id = $1;

-- name: ListAssets :many
SELECT * FROM assets WHERE project_id = $1 ORDER BY created_at DESC;

-- name: ListAllProjects :many
SELECT * FROM projects ORDER BY created_at DESC;

-- name: ListProjectMembers :many
SELECT pm.project_id, pm.user_id, pm.role, pm.added_at, u.email, u.display_name
FROM project_members pm JOIN users u ON u.id = pm.user_id
WHERE pm.project_id = $1
ORDER BY pm.added_at;
