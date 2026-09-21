-- ON CONFLICT DO NOTHING: (content_id, reporter_id) uniqueness is enforced in
-- the schema, so a repeat report by the same user inserts nothing and cannot
-- inflate any count.
-- name: InsertReport :one
INSERT INTO reports (content_id, reporter_id, reason)
VALUES ($1, $2, $3)
ON CONFLICT (content_id, reporter_id) DO NOTHING
RETURNING *;

-- name: GetReport :one
SELECT * FROM reports
WHERE content_id = $1 AND reporter_id = $2;

-- name: ListReportsForContent :many
SELECT * FROM reports
WHERE content_id = $1
ORDER BY id DESC;

-- name: CountReportsForContent :one
SELECT count(*)::int AS cnt FROM reports WHERE content_id = $1;

-- name: ResolveReport :exec
UPDATE reports SET status = 'resolved' WHERE id = $1;

-- name: QueryReportContent :one
SELECT content_id FROM reports WHERE id = $1;
