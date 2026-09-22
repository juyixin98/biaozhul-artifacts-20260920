-- name: CreateAlert :one
INSERT INTO alerts (
    org_id, rule_version_id, rule_type, fingerprint, status, title, detail,
    resolved_by
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (org_id, fingerprint) DO NOTHING
RETURNING *;

-- name: GetAlert :one
SELECT * FROM alerts WHERE org_id = $1 AND id = $2;

-- name: GetAlertByFingerprint :one
SELECT * FROM alerts WHERE org_id = $1 AND fingerprint = $2;

-- name: ListAlerts :many
SELECT * FROM alerts
WHERE org_id = $1
  AND (sqlc.narg(filter_status)::text IS NULL OR status = sqlc.narg(filter_status))
ORDER BY id DESC
LIMIT $2 OFFSET $3;

-- name: CountAlerts :one
SELECT count(*) FROM alerts
WHERE org_id = $1
  AND (sqlc.narg(filter_status)::text IS NULL OR status = sqlc.narg(filter_status));

-- Optimistic transition: succeeds only when the caller's expected version matches.
-- name: TransitionAlert :one
UPDATE alerts
SET status = sqlc.arg(to_status),
    version = version + 1,
    updated_at = now(),
    resolved_by = CASE WHEN sqlc.arg(to_status) IN ('resolved','false_positive')
                       THEN sqlc.arg(resolved_by) ELSE resolved_by END,
    detail = COALESCE(sqlc.arg(detail_json)::jsonb, detail)
WHERE id = sqlc.arg(alert_id) AND org_id = sqlc.arg(org_id) AND version = sqlc.arg(expected_version)
RETURNING *;

-- name: InsertAlertEvent :exec
INSERT INTO alert_events (alert_id, event_id, added_by_revision)
VALUES ($1, $2, $3)
ON CONFLICT (alert_id, event_id) DO UPDATE SET added_by_revision = EXCLUDED.added_by_revision;

-- name: ListAlertEvents :many
SELECT e.* FROM alert_events ae JOIN events e ON e.id = ae.event_id
WHERE ae.alert_id = $1 ORDER BY e.occurred_at, e.id;

-- name: InsertStatusHistory :exec
INSERT INTO alert_status_history (alert_id, from_status, to_status, note, acted_by)
VALUES ($1, $2, $3, $4, $5);

-- name: ListStatusHistory :many
SELECT * FROM alert_status_history WHERE alert_id = $1 ORDER BY id;

-- name: InsertEvidenceRevision :exec
INSERT INTO alert_evidence_revisions (alert_id, seq, event_count, detail)
VALUES ($1, $2, $3, $4);

-- name: NextEvidenceSeq :one
SELECT COALESCE(max(seq), 0) + 1 AS next_seq FROM alert_evidence_revisions WHERE alert_id = $1;

-- name: ListEvidenceRevisions :many
SELECT * FROM alert_evidence_revisions WHERE alert_id = $1 ORDER BY seq;

-- name: LatestEvidenceSeq :one
SELECT COALESCE(max(seq), 0)::int AS latest_seq, COALESCE(max(event_count), 0)::int AS latest_count
FROM alert_evidence_revisions WHERE alert_id = $1;
