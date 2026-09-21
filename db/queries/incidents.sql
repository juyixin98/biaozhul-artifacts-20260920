-- name: CreateIncident :one
INSERT INTO incidents (title, severity, created_by)
VALUES (sqlc.arg(title), sqlc.arg(severity)::severity_level, sqlc.arg(created_by))
RETURNING *;

-- name: GetIncident :one
SELECT * FROM incidents WHERE id = $1;

-- name: GetIncidentForUpdate :one
SELECT * FROM incidents WHERE id = $1 FOR UPDATE;

-- name: ListIncidents :many
SELECT * FROM incidents ORDER BY created_at DESC;

-- name: AdvanceIncident :one
UPDATE incidents
SET status = sqlc.arg(status)::incident_status, version = version + 1, updated_at = now()
WHERE id = sqlc.arg(id) AND version = sqlc.arg(expected_version)
RETURNING *;

-- name: SetPostmortem :one
UPDATE incidents
SET root_cause = sqlc.arg(root_cause), lessons_learned = sqlc.arg(lessons_learned),
    version = version + 1, updated_at = now()
WHERE id = sqlc.arg(id)
RETURNING *;

-- name: TryIncrementEvidenceCount :one
UPDATE incidents
SET evidence_count = evidence_count + 1, updated_at = now()
WHERE id = $1 AND evidence_count < 50
RETURNING evidence_count;
