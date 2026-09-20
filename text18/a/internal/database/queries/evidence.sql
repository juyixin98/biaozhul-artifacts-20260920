-- Evidence is append-only. Every insert happens while the incident row is
-- held FOR UPDATE, so concurrent submits serialize and the 50-item cap
-- cannot be bypassed by a race.

-- name: MaxEvidenceSeq :one
SELECT COALESCE(MAX(seq), 0)::integer AS max_seq
FROM evidence
WHERE incident_id = $1;

-- name: CreateEvidence :one
INSERT INTO evidence (incident_id, seq, content, submitted_by, request_id)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetEvidence :one
SELECT * FROM evidence
WHERE id = $1;

-- name: ListEvidence :many
SELECT * FROM evidence
WHERE incident_id = $1
ORDER BY seq;

-- name: ListEvidenceWithAuthor :many
SELECT e.*, u.username AS author_username, u.full_name AS author_full_name
FROM evidence e
JOIN users u ON u.id = e.submitted_by
WHERE e.incident_id = $1
ORDER BY e.seq;

-- Corrections may only be appended as linked notes; the original content
-- is never overwritten.
-- name: CreateEvidenceNote :one
INSERT INTO evidence_notes (evidence_id, incident_id, author_id, note)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: ListNotesByEvidence :many
SELECT * FROM evidence_notes
WHERE evidence_id = $1
ORDER BY created_at, id;

-- name: ListNotesByIncidentWithAuthor :many
SELECT n.*, u.username AS author_username, u.full_name AS author_full_name
FROM evidence_notes n
JOIN users u ON u.id = n.author_id
WHERE n.incident_id = $1
ORDER BY n.created_at, n.id;
