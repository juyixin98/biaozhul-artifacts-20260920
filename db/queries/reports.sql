-- name: CreateReport :one
INSERT INTO reports (community_id, reporter_id, post_id, target_version_id,
                     category, reason, status)
VALUES ($1, $2, $3, $4, $5, $6, 'filed')
RETURNING id, community_id, reporter_id, post_id, target_version_id, category,
          reason, status, created_at;

-- name: GetReportForUpdate :one
SELECT id, community_id, reporter_id, post_id, target_version_id, category,
       reason, status, created_at
FROM reports WHERE id = $1 FOR UPDATE;

-- name: GetReport :one
SELECT id, community_id, reporter_id, post_id, target_version_id, category,
       reason, status, created_at
FROM reports WHERE id = $1;

-- name: ListReports :many
SELECT id, community_id, reporter_id, post_id, target_version_id, category,
       reason, status, created_at
FROM reports
WHERE community_id = $1
  AND (sqlc.narg('status')::text IS NULL OR status = sqlc.narg('status'))
ORDER BY id DESC LIMIT $2 OFFSET $3;

-- name: SetReportStatus :one
UPDATE reports SET status = sqlc.arg('status') WHERE id = sqlc.arg('id')
RETURNING *;

-- name: InsertDecision :one
INSERT INTO report_decisions (report_id, action, actor_id, reason)
VALUES ($1, $2, $3, $4)
RETURNING id, report_id, action, actor_id, reason, created_at;

-- name: ListDecisions :many
SELECT id, report_id, action, actor_id, reason, created_at
FROM report_decisions WHERE report_id = $1 ORDER BY id;

-- name: CountAppeals :one
SELECT count(*) AS cnt
FROM report_decisions
WHERE report_id = $1 AND action = 'appeal';
