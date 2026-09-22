-- name: InsertAlert :one
INSERT INTO alerts (
    org_id, rule_id, rule_version, kind, status, fingerprint,
    window_start, window_end, db_user, event_id, event_pk, event_count
) VALUES (
    $1, $2, $3, $4, 'open', $5,
    $6, $7, $8, $9, $10, $11
)
ON CONFLICT (org_id, fingerprint) DO NOTHING
RETURNING *;

-- name: GetAlertByFingerprint :one
SELECT * FROM alerts WHERE org_id = $1 AND fingerprint = $2;

-- name: GetAlert :one
SELECT * FROM alerts WHERE org_id = $1 AND id = $2;

-- name: LinkAlertEvent :exec
INSERT INTO alert_events (alert_id, event_pk)
VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: CountAlertEvents :one
SELECT count(*)::int AS cnt FROM alert_events WHERE alert_id = $1;

-- name: InsertAlertRevision :exec
INSERT INTO alert_revisions (
    alert_id, revision, from_status, to_status, note, event_count, actor_id
) VALUES ($1, $2, $3, $4, $5, $6, $7);

-- name: ListAlertRevisions :many
SELECT r.id, r.alert_id, r.revision, r.from_status, r.to_status, r.note,
       r.event_count, r.actor_id, r.created_at,
       u.display_name AS actor_name
FROM alert_revisions r
LEFT JOIN users u ON u.id = r.actor_id
WHERE r.alert_id = $1
ORDER BY r.id;

-- name: ListAlerts :many
SELECT * FROM alerts
WHERE org_id = $1
  AND (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status'))
ORDER BY id DESC
LIMIT $2 OFFSET $3;

-- name: CountAlerts :one
SELECT count(*) AS cnt FROM alerts
WHERE org_id = $1
  AND (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status'));

-- Optimistic status transition. Only succeeds when the caller's expected
-- version still matches; the version bump and history insert stay atomic
-- because the caller wraps them in one transaction.
-- name: TransitionAlert :one
UPDATE alerts
SET status = $3, version = version + 1, updated_at = now(),
    assigned_to = COALESCE($4, assigned_to)
WHERE id = $1 AND org_id = $2 AND version = $5
RETURNING *;

-- Analyst self-assignment when starting an investigation.
-- name: AssignAlert :one
UPDATE alerts
SET assigned_to = $3, version = version + 1, updated_at = now()
WHERE id = $1 AND org_id = $2 AND version = $4
RETURNING *;

-- Recomputation path: counters only, never touches status/assignment/version.
-- name: BumpAlertCount :exec
UPDATE alerts SET event_count = $3, updated_at = now()
WHERE id = $1 AND org_id = $2;

-- name: LinkedEventPks :many
SELECT event_pk FROM alert_events WHERE alert_id = $1;
