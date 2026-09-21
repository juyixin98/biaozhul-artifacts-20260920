-- name: InsertAuditEvent :one
INSERT INTO audit_events (incident_id, actor, action, from_status, to_status, detail, request_id)
VALUES (sqlc.arg(incident_id), sqlc.arg(actor), sqlc.arg(action),
        sqlc.narg(from_status)::incident_status, sqlc.narg(to_status)::incident_status,
        sqlc.arg(detail), sqlc.narg(request_id))
RETURNING *;

-- name: ListAuditEvents :many
SELECT * FROM audit_events WHERE incident_id = $1 ORDER BY id;
