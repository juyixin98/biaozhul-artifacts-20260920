-- name: AddEvidence :one
INSERT INTO evidence (id, incident_id, content, submitted_by, request_id)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: CountEvidence :one
SELECT count(*)::int AS cnt FROM evidence WHERE incident_id = $1;

-- name: GetEvidence :one
SELECT * FROM evidence WHERE id = $1;

-- name: ListEvidence :many
SELECT * FROM evidence WHERE incident_id = $1 ORDER BY created_at, id;

-- name: ListEvidenceWithNotes :many
SELECT
    e.id AS evidence_id,
    e.content AS evidence_content,
    e.submitted_by AS evidence_submitted_by,
    e.created_at AS evidence_created_at,
    n.id AS note_id,
    n.content AS note_content,
    n.added_by AS note_added_by,
    n.created_at AS note_created_at
FROM evidence e
LEFT JOIN evidence_notes n ON n.evidence_id = e.id
WHERE e.incident_id = $1
ORDER BY e.created_at, e.id, n.created_at;

-- name: AddEvidenceNote :one
INSERT INTO evidence_notes (id, evidence_id, content, added_by, request_id)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;
