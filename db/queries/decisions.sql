-- name: InsertDecision :one
INSERT INTO moderation_decisions
    (content_id, revision_id, rule_version_id, state, moderator_id, reason)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- An approval is usable for publishing ONLY when it was made against the
-- revision the content is currently showing. Stale approvals never match.
-- name: FindApprovalForRevision :one
SELECT * FROM moderation_decisions
WHERE content_id = $1
  AND revision_id = $2
  AND state = 'approved'
ORDER BY id DESC
LIMIT 1;

-- name: ListDecisionsForContent :many
SELECT * FROM moderation_decisions
WHERE content_id = $1
ORDER BY id DESC;
