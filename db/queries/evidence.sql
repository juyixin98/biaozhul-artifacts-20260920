-- name: InsertEvidence :one
INSERT INTO evidence (incident_id, seq, content, submitted_by)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetEvidence :one
SELECT * FROM evidence WHERE id = $1;

-- name: ListEvidence :many
SELECT * FROM evidence WHERE incident_id = $1 ORDER BY seq;

-- name: AddEvidenceNote :one
INSERT INTO evidence_notes (evidence_id, note, created_by)
VALUES ($1, $2, $3)
RETURNING *;

-- name: ListNotesForIncident :many
SELECT n.* FROM evidence_notes n
JOIN evidence e ON e.id = n.evidence_id
WHERE e.incident_id = $1
ORDER BY n.id;
