-- name: AddMember :exec
INSERT INTO incident_members (incident_id, user_id, role, assigned_by)
VALUES (sqlc.arg(incident_id), sqlc.arg(user_id), sqlc.arg(role)::member_role, sqlc.arg(assigned_by))
ON CONFLICT (incident_id, user_id, role) DO NOTHING;

-- name: GetMember :one
SELECT * FROM incident_members
WHERE incident_id = sqlc.arg(incident_id) AND user_id = sqlc.arg(user_id) AND role = sqlc.arg(role)::member_role;

-- name: ListMembers :many
SELECT * FROM incident_members WHERE incident_id = $1 ORDER BY assigned_at;

-- name: CountResponders :one
SELECT count(*) FROM incident_members
WHERE incident_id = $1 AND role = 'responder';
