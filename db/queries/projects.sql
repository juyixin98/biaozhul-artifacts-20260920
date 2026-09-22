-- name: CreateProject :one
INSERT INTO projects (name, owner_id)
VALUES ($1, $2)
RETURNING *;

-- name: GetProject :one
SELECT * FROM projects WHERE id = $1;

-- name: ListProjectsForUser :many
SELECT p.*
FROM projects p
JOIN project_members pm ON pm.project_id = p.id
WHERE pm.user_id = $1
ORDER BY p.created_at;

-- name: IsProjectMember :one
SELECT EXISTS (
    SELECT 1 FROM project_members
    WHERE project_id = $1 AND user_id = $2
) AS is_member;

-- name: AddProjectMember :exec
INSERT INTO project_members (project_id, user_id)
VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: ListProjectMembers :many
SELECT u.*
FROM users u
JOIN project_members pm ON pm.user_id = u.id
WHERE pm.project_id = $1
ORDER BY u.username;

-- name: ListAllProjects :many
SELECT * FROM projects ORDER BY created_at;
