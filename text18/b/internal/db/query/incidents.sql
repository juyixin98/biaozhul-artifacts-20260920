-- name: CreateIncident :one
INSERT INTO incidents (id, title, severity, created_by, assigned_analyst_id, assigned_responder_id)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: GetIncident :one
SELECT * FROM incidents WHERE id = $1;

-- name: GetIncidentForUpdate :one
SELECT * FROM incidents WHERE id = $1 FOR UPDATE;

-- name: ListIncidents :many
SELECT * FROM incidents
WHERE (@assignee_filter::boolean = false
       OR assigned_analyst_id = @user_id
       OR assigned_responder_id = @user_id)
  AND (@severity::text = '' OR severity = @severity)
ORDER BY created_at DESC, id
LIMIT $1 OFFSET $2;

-- name: AddIncidentMember :exec
INSERT INTO incident_members (incident_id, user_id) VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: IsIncidentMember :one
SELECT EXISTS(
    SELECT 1 FROM incident_members WHERE incident_id = $1 AND user_id = $2
) AS member;

-- name: ListIncidentMembers :many
SELECT u.* FROM users u
JOIN incident_members im ON im.user_id = u.id
WHERE im.incident_id = $1
ORDER BY u.id;

-- name: AssignIncidentPerson :exec
UPDATE incidents
SET assigned_analyst_id   = CASE WHEN @role::text = 'analyst'   THEN @user_id ELSE assigned_analyst_id END,
    assigned_responder_id = CASE WHEN @role::text = 'responder' THEN @user_id ELSE assigned_responder_id END,
    updated_at = now()
WHERE id = $1;

-- Atomic forward transition. The WHERE clause enforces: expected version
-- (optimistic lock), exact current stage (no skipped transitions), and the
-- P1 triage gate. Returns the updated row; no row means the caller must
-- distinguish version mismatch from stage mismatch.
-- name: TransitionIncident :one
UPDATE incidents AS inc
    SET stage     = @to_stage,
        version   = version + 1,
        triaged_at    = CASE WHEN @to_stage = 'triaged'    THEN now() ELSE triaged_at END,
        contained_at  = CASE WHEN @to_stage = 'contained'  THEN now() ELSE contained_at END,
        eradicated_at = CASE WHEN @to_stage = 'eradicated' THEN now() ELSE eradicated_at END,
        recovered_at  = CASE WHEN @to_stage = 'recovered'  THEN now() ELSE recovered_at END,
        postmortem_at = CASE WHEN @to_stage = 'postmortem' THEN now() ELSE postmortem_at END,
        closed_at     = CASE WHEN @to_stage = 'closed'     THEN now() ELSE closed_at END,
        root_cause      = COALESCE(@root_cause, root_cause),
        lessons_learned = COALESCE(@lessons_learned, lessons_learned),
        updated_at = now()
    WHERE inc.id = @incident_id
      AND inc.version = @expected_version
      AND inc.stage = @from_stage
      AND NOT (@from_stage = 'detected'
               AND inc.severity = 'P1'
               AND inc.assigned_responder_id IS NULL)
      AND NOT (@to_stage = 'closed'
               AND (inc.root_cause IS NULL OR inc.root_cause = ''
                    OR inc.lessons_learned IS NULL OR inc.lessons_learned = ''
                    OR NOT EXISTS (SELECT 1 FROM action_items ai
                                   WHERE ai.incident_id = inc.id)))
RETURNING inc.*;

-- name: InsertStageEvent :one
INSERT INTO stage_events (id, incident_id, version, from_stage, to_stage, actor_id, note, request_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: ListStageEvents :many
SELECT * FROM stage_events WHERE incident_id = $1 ORDER BY version, occurred_at;

-- name: ListIncidentsForExport :many
SELECT * FROM incidents ORDER BY created_at;

-- name: CountOpenActionItems :one
SELECT count(*)::int AS open_count FROM action_items
WHERE incident_id = $1 AND status = 'open';

-- name: CountActionItemsAny :one
SELECT EXISTS (SELECT 1 FROM action_items WHERE incident_id = $1) AS any_items;
