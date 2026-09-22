-- name: CreateContent :one
INSERT INTO contents (category_id, author_id, title, status, current_revision)
VALUES ($1, $2, $3, 'draft', $4) RETURNING *;

-- name: CreateRevision :one
INSERT INTO content_revisions
    (content_id, revision_no, body, edit_reason, origin, source_revision_id, author_id)
VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *;

-- name: GetContent :one
SELECT * FROM contents WHERE id = $1;

-- name: GetContentForUpdate :one
SELECT * FROM contents WHERE id = $1 FOR UPDATE;

-- name: SetCurrentRevision :exec
UPDATE contents SET current_revision = $2, updated_at = now() WHERE id = $1;

-- name: SetContentStatus :exec
UPDATE contents SET status = $2, updated_at = now() WHERE id = $1;

-- name: PublishRevision :exec
UPDATE contents
SET status = 'published', current_revision = $2, published_revision = $2, updated_at = now()
WHERE id = $1;

-- name: GetRevision :one
SELECT * FROM content_revisions WHERE id = $1;

-- name: GetRevisionByNo :one
SELECT * FROM content_revisions WHERE content_id = $1 AND revision_no = $2;

-- name: MaxRevisionNo :one
SELECT COALESCE(MAX(revision_no), 0)::INTEGER AS revision_no
FROM content_revisions WHERE content_id = $1;

-- name: ListRevisions :many
SELECT * FROM content_revisions
WHERE content_id = $1 ORDER BY revision_no DESC;

-- name: InsertEvent :exec
INSERT INTO status_events (content_id, revision_id, actor_id, event, from_status, to_status, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7);

-- name: ListEvents :many
SELECT * FROM status_events WHERE content_id = $1 ORDER BY id;

-- name: ListUserContents :many
SELECT * FROM contents WHERE author_id = $1 ORDER BY id DESC;
