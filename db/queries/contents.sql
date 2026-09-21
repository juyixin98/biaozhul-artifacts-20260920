-- name: CreateContent :one
INSERT INTO contents (category, author_id, title, status, current_revision_id)
VALUES ($1, $2, $3, 'draft', NULL)
RETURNING *;

-- name: GetContent :one
SELECT * FROM contents WHERE id = $1;

-- name: GetContentForUpdate :one
SELECT * FROM contents WHERE id = $1 FOR UPDATE;

-- name: InsertRevision :one
INSERT INTO content_revisions (content_id, revision_no, body, edit_reason, created_by)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetRevision :one
SELECT * FROM content_revisions WHERE id = $1;

-- name: ListRevisions :many
SELECT * FROM content_revisions
WHERE content_id = $1
ORDER BY revision_no DESC;

-- name: NextRevisionNo :one
SELECT COALESCE(MAX(revision_no), 0)::bigint AS next_no
FROM content_revisions
WHERE content_id = $1;

-- name: AttachRevision :exec
UPDATE contents SET current_revision_id = $2, updated_at = now()
WHERE id = $1;

-- name: SetContentStatus :exec
UPDATE contents SET status = $2, updated_at = now()
WHERE id = $1;

-- name: PublishContent :exec
UPDATE contents SET status = 'published', current_revision_id = $2, updated_at = now()
WHERE id = $1;

-- Keyset feed: status='published', ordered by (updated_at DESC, id DESC).
-- Withdrawals remove rows; updated rows stay anchored at their old position
-- unless their updated_at changes, so a page boundary never leaks unpublished work.

-- name: FeedFirstPage :many
SELECT * FROM contents
WHERE status = 'published'
ORDER BY updated_at DESC, id DESC
LIMIT $1;

-- name: FeedNextPage :many
SELECT * FROM contents
WHERE status = 'published'
  AND (updated_at, id) < (sqlc.arg(after_updated_at)::timestamptz, sqlc.arg(after_id)::bigint)
ORDER BY updated_at DESC, id DESC
LIMIT sqlc.arg(row_limit);

-- name: UpdateContentTitle :exec
UPDATE contents SET title = $2, updated_at = now() WHERE id = $1;
