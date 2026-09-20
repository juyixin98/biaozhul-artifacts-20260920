-- name: GetUser :one
SELECT * FROM users WHERE id = $1;

-- name: CreateIncident :one
INSERT INTO incidents (id, title, description, severity, created_by)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetIncident :one
SELECT * FROM incidents WHERE id = $1;

-- name: GetIncidentForUpdate :one
SELECT * FROM incidents WHERE id = $1 FOR UPDATE;

-- name: ListIncidents :many
SELECT * FROM incidents ORDER BY created_at DESC, id DESC;

-- name: UpdateIncidentStatus :one
UPDATE incidents
SET status = $2, version = $3, updated_at = now()
WHERE id = $1
RETURNING *;

-- name: AssignResponder :one
UPDATE incidents
SET assignee_id = $2, version = version + 1, updated_at = now()
WHERE id = $1
RETURNING *;

-- name: UpdatePostmortem :one
UPDATE incidents
SET root_cause = $2, lessons_learned = $3, updated_at = now()
WHERE id = $1
RETURNING *;

-- name: GetTransitionByRequestID :one
SELECT * FROM phase_transitions WHERE incident_id = $1 AND request_id = $2;

-- name: InsertTransition :one
INSERT INTO phase_transitions (id, incident_id, request_id, from_status, to_status, actor_id, note, version_after)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: ListTransitionsByIncident :many
SELECT * FROM phase_transitions WHERE incident_id = $1 ORDER BY created_at ASC, id ASC;

-- name: InsertAuditEvent :one
INSERT INTO audit_events (id, incident_id, actor_id, event_type, payload)
VALUES ($1, $2, $3, $4, sqlc.arg(payload)::jsonb)
RETURNING *;

-- name: ListAuditEventsByIncident :many
SELECT * FROM audit_events WHERE incident_id = $1 ORDER BY created_at ASC, id ASC;

-- name: CountEvidence :one
SELECT count(*)::int FROM evidence WHERE incident_id = $1;

-- name: InsertEvidence :one
INSERT INTO evidence (id, incident_id, seq, content, author_id)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: ListEvidenceByIncident :many
SELECT * FROM evidence WHERE incident_id = $1 ORDER BY seq ASC;

-- name: GetEvidence :one
SELECT * FROM evidence WHERE id = $1;

-- name: InsertEvidenceNote :one
INSERT INTO evidence_notes (id, evidence_id, content, author_id)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: ListEvidenceNotesByEvidence :many
SELECT * FROM evidence_notes WHERE evidence_id = $1 ORDER BY created_at ASC, id ASC;

-- name: ListEvidenceNotesByIncident :many
SELECT en.* FROM evidence_notes en
JOIN evidence e ON e.id = en.evidence_id
WHERE e.incident_id = $1
ORDER BY en.created_at ASC, en.id ASC;

-- name: CreateActionItem :one
INSERT INTO action_items (id, incident_id, title, owner_id, due_at)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetActionItem :one
SELECT * FROM action_items WHERE id = $1;

-- name: GetActionItemForUpdate :one
SELECT * FROM action_items WHERE id = $1 FOR UPDATE;

-- name: ListActionItemsByIncident :many
SELECT * FROM action_items WHERE incident_id = $1 ORDER BY created_at ASC, id ASC;

-- name: CountActionItems :one
SELECT count(*)::int FROM action_items WHERE incident_id = $1;

-- name: RescheduleActionItem :one
UPDATE action_items
SET due_at = $2, due_version = due_version + 1, updated_at = now()
WHERE id = $1
RETURNING *;

-- name: CompleteActionItem :one
UPDATE action_items
SET status = 'done', updated_at = now()
WHERE id = $1
RETURNING *;

-- name: ListDueActionItems :many
SELECT * FROM action_items
WHERE status = 'open' AND due_at <= $1 AND reminded_due_version < due_version
ORDER BY due_at ASC;

-- name: MarkActionItemReminded :exec
UPDATE action_items
SET reminded_due_version = due_version, updated_at = now()
WHERE id = $1 AND due_version = $2 AND reminded_due_version < due_version;

-- name: InsertReminder :execrows
INSERT INTO reminders (id, action_item_id, due_version)
VALUES ($1, $2, $3)
ON CONFLICT (action_item_id, due_version) DO NOTHING;

-- name: ListRemindersByIncident :many
SELECT r.* FROM reminders r
JOIN action_items ai ON ai.id = r.action_item_id
WHERE ai.incident_id = $1
ORDER BY r.sent_at ASC, r.id ASC;
