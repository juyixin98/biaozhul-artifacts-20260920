-- name: InsertEvent :one
INSERT INTO status_events
    (content_id, revision_id, rule_version_id, from_status, to_status, actor_id, actor_role, reason)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING *;

-- name: ListEvents :many
SELECT * FROM status_events
WHERE content_id = $1
ORDER BY id DESC
LIMIT $2;
