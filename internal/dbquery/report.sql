-- name: CreateReport :one
INSERT INTO reports (community_id, content_id, version_id, reporter_id, category, reason)
VALUES ($1, $2, $3, $4, $5, $6) RETURNING *;

-- name: GetReport :one
SELECT * FROM reports WHERE id = $1 AND community_id = $2;

-- name: GetReportForUpdate :one
SELECT * FROM reports WHERE id = $1 AND community_id = $2 FOR UPDATE;

-- name: ListReports :many
SELECT * FROM reports
WHERE community_id = $1
  AND (sqlc.narg(status)::text IS NULL OR status = sqlc.narg(status))
ORDER BY id DESC;

-- name: InsertReportEvent :exec
INSERT INTO report_events (report_id, community_id, actor_id, action, basis)
VALUES ($1, $2, $3, $4, $5);

-- name: ListReportEvents :many
SELECT * FROM report_events WHERE report_id = $1 AND community_id = $2 ORDER BY id;

-- 受理: valid only while the report is still in the initial accepted state.
-- name: AcceptReport :one
UPDATE reports SET status = 'accepted'
WHERE id = $1 AND community_id = $2 AND status = 'accepted'
RETURNING *;

-- First-instance ruling against content (accepted -> upheld).
-- name: UpholdReport :one
UPDATE reports SET status = 'upheld'
WHERE id = $1 AND community_id = $2 AND status = 'accepted'
RETURNING *;

-- First-instance ruling for the content (accepted -> dismissed).
-- name: DismissReport :one
UPDATE reports SET status = 'dismissed'
WHERE id = $1 AND community_id = $2 AND status = 'accepted'
RETURNING *;

-- One appeal only; flips the report back to the accepted/pending state so the
-- ruling endpoints can decide it a second time.
-- name: AppealReport :one
UPDATE reports SET appealed = TRUE, status = 'accepted'
WHERE id = $1 AND community_id = $2
  AND appealed = FALSE
  AND status IN ('upheld','dismissed')
RETURNING *;

-- Appeal ruling against the appellant.
-- name: DecideAppealUpheld :one
UPDATE reports SET status = 'appeal_upheld'
WHERE id = $1 AND community_id = $2
  AND appealed = TRUE AND status = 'accepted'
RETURNING *;

-- Successful appeal; content may then be explicitly restored.
-- name: DecideAppealDismissed :one
UPDATE reports SET status = 'appeal_dismissed'
WHERE id = $1 AND community_id = $2
  AND appealed = TRUE AND status = 'accepted'
RETURNING *;
