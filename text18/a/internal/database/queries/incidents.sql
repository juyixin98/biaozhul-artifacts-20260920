-- name: CreateIncident :one
INSERT INTO incidents (title, description, severity, created_by)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: CreateIncidentWithID :one
INSERT INTO incidents (id, title, description, severity, created_by, detected_at, created_at, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $6, $6)
RETURNING *;

-- name: GetIncident :one
SELECT * FROM incidents
WHERE id = $1;

-- name: GetIncidentForUpdate :one
SELECT * FROM incidents
WHERE id = $1
FOR UPDATE;

-- name: ListIncidents :many
SELECT * FROM incidents
WHERE ($1::boolean = true
       OR EXISTS (SELECT 1 FROM incident_members m
                  WHERE m.incident_id = incidents.id AND m.user_id = $2::uuid))
  AND ($3::text = '' OR status = $3)
ORDER BY created_at DESC, id
LIMIT $4 OFFSET $5;

-- Atomic optimistic forward step: no row returned means the expected version is stale.
-- name: AdvanceIncident :one
UPDATE incidents
SET status = $3,
    version = version + 1,
    root_cause = COALESCE($4, root_cause),
    lessons_learned = COALESCE($5, lessons_learned),
    closed_at = CASE WHEN $3 = 'closed' THEN $6 ELSE closed_at END,
    updated_at = $6
WHERE id = $1 AND version = $2
RETURNING *;

-- name: AddMember :exec
INSERT INTO incident_members (incident_id, user_id, case_role, assigned_by)
VALUES ($1, $2, $3, $4)
ON CONFLICT (incident_id, user_id) DO UPDATE
SET case_role = EXCLUDED.case_role,
    assigned_by = EXCLUDED.assigned_by;

-- name: GetMember :one
SELECT * FROM incident_members
WHERE incident_id = $1 AND user_id = $2;

-- name: ListMembers :many
SELECT m.*, u.username, u.full_name
FROM incident_members m
JOIN users u ON u.id = m.user_id
WHERE m.incident_id = $1
ORDER BY u.username;

-- name: CountMembersByRole :one
SELECT count(*)::bigint AS member_count
FROM incident_members
WHERE incident_id = $1 AND case_role = $2;

-- name: CreatePhase :one
INSERT INTO incident_phases (incident_id, phase, actor_id, request_id, entered_at)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: ListPhases :many
SELECT * FROM incident_phases
WHERE incident_id = $1
ORDER BY entered_at,
         array_position(ARRAY['detection','triage','containment','eradication',
                               'recovery','review','closure'], phase),
         id;

-- name: ListPhasesWithActor :many
SELECT p.*, u.username AS actor_username, u.full_name AS actor_full_name
FROM incident_phases p
JOIN users u ON u.id = p.actor_id
WHERE p.incident_id = $1
ORDER BY p.entered_at,
         array_position(ARRAY['detection','triage','containment','eradication',
                               'recovery','review','closure'], p.phase),
         p.id;

-- name: CreateAuditEvent :one
INSERT INTO audit_events (incident_id, actor_id, action, from_status, to_status, request_id, detail)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: ListAuditByIncident :many
SELECT * FROM audit_events
WHERE incident_id = $1
ORDER BY created_at, id;
