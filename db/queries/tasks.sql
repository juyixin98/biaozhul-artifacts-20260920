-- name: InsertReviewTask :one
INSERT INTO review_tasks (content_id, revision_id, rule_version_id, state)
VALUES ($1, $2, $3, 'pending')
RETURNING *;

-- name: GetReviewTask :one
SELECT * FROM review_tasks WHERE id = $1;

-- name: FindOpenTaskForContent :one
SELECT * FROM review_tasks
WHERE content_id = $1 AND state IN ('pending', 'claimed')
ORDER BY id DESC
LIMIT 1;

-- Atomic claim scoped to the moderator's categories: only one moderator wins
-- a given task; already-expired claims are reclaimable. A NULL category array
-- means "all categories" (admin). FOR UPDATE SKIP LOCKED lets concurrent
-- claimants for different tasks never block or duplicate each other.
-- name: ClaimPendingTask :one
WITH candidate AS (
    SELECT t.id
    FROM review_tasks t
    JOIN contents c ON c.id = t.content_id
    WHERE (t.state = 'pending'
           OR (t.state = 'claimed' AND t.claim_expires_at < now()))
      AND (sqlc.arg(categories)::text[] IS NULL
           OR c.category = ANY(sqlc.arg(categories)::text[]))
    ORDER BY t.created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE review_tasks t
SET state = 'claimed',
    claimed_by = $1,
    claimed_at = now(),
    claim_expires_at = now() + make_interval(secs => $2),
    decision_reason = '',
    decided_by = NULL
FROM candidate
WHERE t.id = candidate.id
RETURNING t.*;

-- Requeue all expired claims. A stale claimer can no longer finish the task.
-- name: RequeueExpiredClaims :many
UPDATE review_tasks
SET state = 'pending', claimed_by = NULL, claimed_at = NULL, claim_expires_at = NULL
WHERE state = 'claimed' AND claim_expires_at < now()
RETURNING *;

-- name: ListQueueTasks :many
SELECT t.* FROM review_tasks t
JOIN contents c ON c.id = t.content_id
WHERE (t.state = 'pending' OR (t.state = 'claimed' AND t.claim_expires_at < now()))
  AND ($1::text = '' OR c.category = $1)
ORDER BY t.created_at
LIMIT $2;

-- Lock the task row for a decision.
-- name: GetReviewTaskForUpdate :one
SELECT * FROM review_tasks WHERE id = $1 FOR UPDATE;

-- Finish a task only if the caller owns a live claim.
-- The state/claim predicates make an expired claimer unable to overwrite
-- whatever happened after expiry (requeue + new claim + decision).
-- name: CompleteClaim :one
UPDATE review_tasks
SET state = $3, decided_by = $2, decision_reason = $4
WHERE id = $1
  AND state = 'claimed'
  AND claimed_by = $2
  AND claim_expires_at >= now()
RETURNING *;

-- Owner/admin cancellation (e.g. content withdrawn or superseded by a new revision).
-- name: CancelOpenTask :exec
UPDATE review_tasks
SET state = 'cancelled'
WHERE id = $1 AND state IN ('pending', 'claimed');

-- name: CancelTasksForContentRevision :exec
UPDATE review_tasks
SET state = 'cancelled'
WHERE content_id = $1
  AND revision_id <> $2
  AND state IN ('pending', 'claimed');
