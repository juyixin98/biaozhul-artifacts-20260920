-- name: CloseOpenPhase :exec
UPDATE phase_records SET exited_at = now()
WHERE incident_id = $1 AND exited_at IS NULL;

-- name: OpenPhase :one
INSERT INTO phase_records (incident_id, phase)
VALUES (sqlc.arg(incident_id), sqlc.arg(phase)::incident_status)
RETURNING *;

-- name: ListPhases :many
SELECT * FROM phase_records WHERE incident_id = $1 ORDER BY id;
