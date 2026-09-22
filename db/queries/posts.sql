-- name: CreatePost :one
INSERT INTO posts (community_id, author_id, required_tier_level, title, status, removed_reason)
VALUES (sqlc.arg('community_id'), sqlc.arg('author_id'), sqlc.arg('required_tier_level'),
        sqlc.arg('title'), 'draft', '')
RETURNING *;

-- name: GetPostForUpdate :one
SELECT * FROM posts WHERE id = $1 FOR UPDATE;

-- name: GetPost :one
SELECT * FROM posts WHERE id = $1;

-- name: ListPosts :many
SELECT * FROM posts
WHERE (community_id = sqlc.narg('community_id') OR sqlc.narg('community_id') IS NULL)
  AND (author_id = sqlc.narg('author_id') OR sqlc.narg('author_id') IS NULL)
  AND (status = sqlc.narg('status') OR sqlc.narg('status') IS NULL)
ORDER BY id DESC
LIMIT sqlc.arg('limit') OFFSET sqlc.arg('offset');

-- name: InsertVersion :one
INSERT INTO post_versions (post_id, version_number, title, body,
                           required_tier_level, author_id, review_status)
VALUES (sqlc.arg('post_id'), sqlc.arg('version_number'), sqlc.arg('title'), sqlc.arg('body'),
        sqlc.arg('required_tier_level'), sqlc.arg('author_id'), sqlc.arg('review_status'))
RETURNING *;

-- name: GetVersion :one
SELECT * FROM post_versions WHERE id = $1;

-- name: ListVersions :many
SELECT * FROM post_versions WHERE post_id = $1 ORDER BY version_number DESC;

-- name: SetVersionStatus :exec
UPDATE post_versions SET review_status = $2 WHERE id = $1;

-- name: NextVersionNumber :one
SELECT COALESCE(max(version_number), 0) + 1 AS next
FROM post_versions WHERE post_id = $1;

-- name: SetPostCurrentVersion :exec
UPDATE posts SET current_version_id = $2, updated_at = now() WHERE id = $1;

-- Draft / withdraw / edit -----------------------------------------------------

-- name: MarkDraft :one
-- Re-edit a post. When the post already had a published version it STAYS
-- published (members keep reading the old approved version during re-review);
-- otherwise it becomes a plain draft.
UPDATE posts
SET status = CASE WHEN published_version_id IS NULL THEN 'draft' ELSE 'published' END,
    updated_at = now(),
    removed_reason = ''
WHERE id = $1 AND community_id = $2
RETURNING *;

-- name: SubmitForReview :one
-- A never-published post becomes 'pending'; a re-edit of a live post stays
-- 'published' (its new current version is marked pending separately).
UPDATE posts p
SET status = CASE WHEN p.published_version_id IS NULL THEN 'pending' ELSE 'published' END,
    updated_at = now()
WHERE p.id = $1
  AND p.community_id = $2
  AND p.status IN ('draft','published')
  AND p.current_version_id IS NOT NULL
RETURNING *;

-- name: SubmitVersionPending :execrows
UPDATE post_versions SET review_status = 'pending'
WHERE id = $1 AND review_status IN ('draft','rejected');

-- name: WithdrawSubmission :one
-- Author pulls a pending submission back to draft.
UPDATE posts
SET status = 'draft', updated_at = now()
WHERE id = $1 AND community_id = $2 AND status = 'pending'
RETURNING *;

-- Moderation actions ----------------------------------------------------------

-- name: ApproveVersion :one
-- Binds the approval to BOTH an expected gate state and the exact
-- current_version_id. Initial review requires status='pending'; a re-review of
-- an edit on a live post runs with status='published' (old version still
-- visible). Either way, matching zero rows means the version the reviewer saw
-- is no longer current -> the new unreviewed content is never published.
UPDATE posts
SET status = 'published',
    published_version_id = current_version_id,
    restored_from_version_id = NULL,
    updated_at = now()
WHERE id = sqlc.arg('id')
  AND community_id = sqlc.arg('community_id')
  AND status = sqlc.arg('expected_status')
  AND current_version_id = sqlc.arg('version_id')
RETURNING *;

-- name: MarkVersionApproved :exec
UPDATE post_versions SET review_status = 'approved'
WHERE id = $1 AND review_status = 'pending';

-- name: RejectPostToDraft :one
-- Rejecting a first submission returns the post to draft. Rejecting a re-edit
-- of a live post leaves the old published version live.
UPDATE posts
SET status = CASE WHEN published_version_id IS NULL THEN 'draft' ELSE 'published' END,
    updated_at = now()
WHERE id = $1 AND community_id = $2 AND status IN ('pending','published')
RETURNING *;

-- name: MarkVersionRejected :exec
UPDATE post_versions SET review_status = 'rejected'
WHERE id = $1 AND review_status = 'pending';

-- name: TakedownPost :one
-- Immediate loss of access: published_version_id is cleared. Applies whether
-- the post is currently published or pending.
UPDATE posts
SET status = 'removed',
    published_version_id = NULL,
    removed_reason = sqlc.arg('reason'),
    updated_at = now()
WHERE id = sqlc.arg('id') AND community_id = sqlc.arg('community_id')
  AND status IN ('published','pending')
RETURNING *;

-- name: TakedownPublishedPost :one
UPDATE posts
SET status = 'removed',
    published_version_id = NULL,
    removed_reason = sqlc.arg('reason'),
    updated_at = now()
WHERE id = sqlc.arg('id') AND community_id = sqlc.arg('community_id')
  AND status = 'published'
RETURNING *;

-- name: RestoreOldVersion :one
-- Re-publish an approved version. The service layer first refuses when a newer
-- version exists, so a restore can never clobber later modifications.
UPDATE posts
SET status = 'published',
    current_version_id = sqlc.arg('version_id'),
    published_version_id = sqlc.arg('version_id'),
    removed_reason = '',
    restored_from_version_id = sqlc.arg('version_id'),
    updated_at = now()
WHERE id = sqlc.arg('id')
  AND community_id = sqlc.arg('community_id')
  AND status = 'removed'
RETURNING *;

-- name: InsertReview :one
INSERT INTO reviews (post_id, version_id, reviewer_id, action, reason,
                     expected_status, success)
VALUES (sqlc.arg('post_id'), sqlc.arg('version_id'), sqlc.arg('reviewer_id'),
        sqlc.arg('action'), sqlc.arg('reason'), sqlc.arg('expected_status'),
        sqlc.arg('success'))
RETURNING *;

-- name: ListReviews :many
SELECT * FROM reviews WHERE post_id = $1 ORDER BY id DESC;

-- Attachments -----------------------------------------------------------------

-- name: AddAttachment :one
INSERT INTO attachments (version_id, filename, content_type, data)
VALUES (sqlc.arg('version_id'), sqlc.arg('filename'),
        sqlc.arg('content_type'), sqlc.arg('data'))
RETURNING *;

-- name: GetAttachment :one
SELECT * FROM attachments WHERE id = $1;

-- name: ListAttachments :many
SELECT id, version_id, filename, content_type, created_at
FROM attachments WHERE version_id = $1 ORDER BY id;
