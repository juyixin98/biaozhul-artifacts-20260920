-- name: CreateContent :one
INSERT INTO contents (community_id, author_id, title, required_level)
VALUES ($1, $2, $3, $4) RETURNING *;

-- name: GetContent :one
SELECT * FROM contents WHERE id = $1 AND community_id = $2;

-- name: GetContentForUpdate :one
SELECT * FROM contents WHERE id = $1 AND community_id = $2 FOR UPDATE;

-- name: ListContents :many
SELECT * FROM contents
WHERE community_id = $1
  AND (sqlc.narg(status)::text IS NULL OR status = sqlc.narg(status))
ORDER BY id DESC;

-- name: NextVersionNo :one
SELECT COALESCE(max(version_no), 0) + 1 AS next_no
FROM content_versions
WHERE content_id = $1;

-- name: CreateVersion :one
INSERT INTO content_versions (content_id, community_id, version_no, body, created_by)
VALUES ($1, $2, $3, $4, $5) RETURNING *;

-- name: GetVersion :one
SELECT * FROM content_versions WHERE id = $1 AND community_id = $2;

-- name: ListVersions :many
SELECT * FROM content_versions
WHERE content_id = $1 AND community_id = $2
ORDER BY version_no DESC;

-- name: AddAttachment :one
INSERT INTO attachments (community_id, version_id, filename, content_type, data)
VALUES ($1, $2, $3, $4, $5) RETURNING id, community_id, version_id, filename, content_type, created_at;

-- name: GetAttachment :one
SELECT * FROM attachments WHERE id = $1 AND community_id = $2;

-- name: ListAttachmentsMeta :many
SELECT id, community_id, version_id, filename, content_type, created_at
FROM attachments WHERE version_id = $1 ORDER BY id;

-- name: ListAttachmentBlobs :many
SELECT id, filename, content_type, data FROM attachments
WHERE version_id = $1 ORDER BY id;

-- name: InsertContentEvent :exec
INSERT INTO content_events
    (community_id, content_id, version_id, actor_id, action, reason)
VALUES ($1, $2, $3, $4, $5, $6);

-- name: ListContentEvents :many
SELECT * FROM content_events
WHERE content_id = $1 AND community_id = $2 ORDER BY id;

-- Author submits current working version for review. Allowed from draft,
-- a previously-pending content (re-submit no-op-ish), or delisted (appeal
-- rework path). Must bind the exact current_version_id.
-- name: SubmitContent :one
UPDATE contents
SET status = 'pending'
WHERE id = $1 AND community_id = $2
  AND current_version_id = $3
  AND status IN ('draft','pending','delisted')
RETURNING *;

-- name: MarkVersionPending :exec
UPDATE content_versions SET review_status = 'pending'
WHERE id = $1 AND community_id = $2 AND review_status IN ('draft','rejected');

-- Moderator approve. Bound to BOTH the submitted version and the expected
-- content status. If an author edited after submission, current_version_id has
-- moved on and this matches zero rows -> 409; the unreviewed new version can
-- never be published.
-- name: ApproveContent :one
UPDATE contents c
SET status = 'published',
    published_version_id = c.current_version_id
WHERE c.id = $1 AND c.community_id = $2
  AND c.status = 'pending'
  AND c.current_version_id = $3
RETURNING *;

-- name: ApproveVersion :exec
UPDATE content_versions SET review_status = 'approved'
WHERE id = $1 AND community_id = $2 AND review_status = 'pending';

-- Reject: content returns to draft; only the bound pending version is rejected.
-- name: RejectContent :one
UPDATE contents
SET status = 'draft'
WHERE id = $1 AND community_id = $2
  AND status = 'pending' AND current_version_id = $3
RETURNING *;

-- name: RejectVersion :exec
UPDATE content_versions SET review_status = 'rejected'
WHERE id = $1 AND community_id = $2 AND review_status = 'pending';

-- Author edit: creates a new current version; any pending content drops back to
-- draft so a pending approve cannot publish the new, unreviewed version.
-- name: EditContentSwapVersion :one
UPDATE contents
SET current_version_id = sqlc.arg(new_version_id),
    status = CASE WHEN status = 'pending' THEN 'draft' ELSE status END,
    title = sqlc.arg(title)
WHERE id = $1 AND community_id = $2 AND current_version_id = sqlc.arg(old_version_id)
RETURNING *;

-- Initial binding right after content creation, when current_version_id is
-- still NULL (NULL = NULL never matches, so it needs its own predicate).
-- name: BindInitialVersion :exec
UPDATE contents
SET current_version_id = $3
WHERE id = $1 AND community_id = $2 AND current_version_id IS NULL;

-- name: DelistContent :one
UPDATE contents
SET status = 'delisted'
WHERE id = $1 AND community_id = $2
  AND status IN ('published','pending')
RETURNING *;

-- Restore safety: only succeeds if the version being restored is still the
-- newest version of the content. A newer edit after the takedown makes this
-- match zero rows -> 409, so restoring an old version never clobbers new work.
-- name: RestoreContent :one
UPDATE contents c
SET status = 'published',
    published_version_id = $3
WHERE c.id = $1 AND c.community_id = $2
  AND c.status = 'delisted'
  AND c.current_version_id = $3
RETURNING *;

