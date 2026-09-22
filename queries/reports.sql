-- name: InsertReport :one
INSERT INTO reports (content_id, reporter_id, reason)
VALUES ($1, $2, $3)
RETURNING *;

-- name: ListReportsForContent :many
SELECT * FROM reports WHERE content_id = $1 ORDER BY id;

-- name: ListAllReports :many
SELECT r.* FROM reports r
ORDER BY r.id DESC LIMIT $1;

-- name: ListReportsInCategories :many
SELECT r.* FROM reports r
JOIN contents c ON c.id = r.content_id
WHERE c.category_id = ANY($1::bigint[])
ORDER BY r.id DESC LIMIT $2;

-- Stable cursor feed. Published content only, keyed on the monotonic
-- surrogate id: rows never move within or across pages when newer rows are
-- inserted or older rows are withdrawn mid-pagination. Joins only against
-- the published revision, so a new (pending) revision created after
-- publication is never leaked through the feed.
-- name: FeedPublished :many
SELECT c.id, c.category_id, c.author_id, c.title, c.status,
       c.current_revision, c.published_revision,
       c.created_at, c.updated_at,
       r.body AS body, r.revision_no AS revision_no
FROM contents c
JOIN content_revisions r ON r.id = c.published_revision
WHERE c.status = 'published' AND c.id < $1
ORDER BY c.id DESC
LIMIT $2;
