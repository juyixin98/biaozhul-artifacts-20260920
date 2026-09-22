-- name: InsertReview :one
INSERT INTO reviews
    (content_id, revision_id, rule_version_id, reviewer_id, decision, reason, matched_words)
VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *;

-- name: ListReviewsForContent :many
SELECT * FROM reviews WHERE content_id = $1 ORDER BY id;

-- name: ListReviewsForRevision :many
SELECT * FROM reviews WHERE revision_id = $1 ORDER BY id;

-- name: UpsertPendingTask :exec
INSERT INTO review_tasks (content_id, revision_id, status)
VALUES ($1, $2, 'open')
ON CONFLICT (content_id) DO UPDATE
SET revision_id = EXCLUDED.revision_id,
    status = 'open',
    updated_at = now();

-- name: CancelTaskForContent :exec
UPDATE review_tasks SET status = 'cancelled', updated_at = now()
WHERE content_id = $1 AND status IN ('open','claimed');

-- name: DeleteClaimForContent :exec
-- Remove any claim attached to a content's task (used when the task is
-- cancelled/reopened so the next claimant never hits the claims PK).
DELETE FROM review_claims
WHERE task_id = (SELECT id FROM review_tasks WHERE content_id = $1);

-- name: GetTaskByContent :one
SELECT * FROM review_tasks WHERE content_id = $1;

-- name: GetTaskForUpdate :one
SELECT * FROM review_tasks WHERE id = $1 FOR UPDATE;

-- name: DeleteClaim :exec
DELETE FROM review_claims WHERE task_id = $1;

-- Claiming first recycles expired claims (RecycleExpiredClaims + DeleteClaim
-- run in the same transaction before the pick below), then claims one open
-- task with FOR UPDATE SKIP LOCKED so concurrent claimants never collide.
-- name: ClaimNextTaskModerator :one
SELECT t.*
FROM review_tasks t
JOIN contents ct ON ct.id = t.content_id
WHERE t.status = 'open'
  AND ct.category_id = ANY($1::bigint[])
ORDER BY t.id
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- name: ClaimNextTaskAdmin :one
SELECT t.*
FROM review_tasks t
WHERE t.status = 'open'
ORDER BY t.id
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- name: MarkTaskClaimed :one
UPDATE review_tasks
SET status = 'claimed', updated_at = now()
WHERE id = $1 AND status = 'open'
RETURNING *;

-- name: InsertClaim :exec
INSERT INTO review_claims (task_id, claimant_id, claimed_at, expires_at)
VALUES ($1, $2, now(), $3);

-- name: GetClaim :one
SELECT * FROM review_claims WHERE task_id = $1;

-- name: CompleteTaskWithGuard :one
-- The WHERE guard is the stale-claim defense: an expired claim (or a claim
-- recycled and re-claimed by someone else) updates 0 rows, so the late
-- moderator's decision cannot overwrite the later resolution.
UPDATE review_tasks t
SET status = 'done', updated_at = now()
FROM review_claims c
WHERE t.id = $1
  AND c.task_id = t.id
  AND c.claimant_id = $2
  AND c.expires_at > now()
  AND t.status = 'claimed'
  AND t.revision_id = $3
RETURNING t.*;

-- name: RecycleExpiredClaims :many
-- Returns task ids whose claims just expired (caller deletes claims + flips
-- status inside the same transaction).
UPDATE review_tasks t
SET status = 'open', updated_at = now()
FROM review_claims c
WHERE c.task_id = t.id
  AND c.expires_at <= now()
  AND t.status = 'claimed'
RETURNING t.id;
