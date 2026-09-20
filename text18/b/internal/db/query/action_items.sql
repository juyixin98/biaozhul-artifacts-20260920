-- name: CreateActionItem :one
INSERT INTO action_items (id, incident_id, title, owner_id, due_at, created_by)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: GetActionItem :one
SELECT * FROM action_items WHERE id = $1;

-- name: GetActionItemForUpdate :one
SELECT * FROM action_items WHERE id = $1 FOR UPDATE;

-- name: ListActionItems :many
SELECT * FROM action_items WHERE incident_id = $1 ORDER BY due_at, id;

-- Optimistic-locked reschedule. WHERE version = $3 makes a stale concurrent
-- reschedule lose, so no two schedules for the same version survive.
-- name: RescheduleActionItem :one
UPDATE action_items
SET due_at = $2,
    version = version + 1,
    updated_at = now()
WHERE id = $1 AND version = $3
RETURNING *;

-- name: UpdateActionItemStatus :one
UPDATE action_items
SET status = $2, updated_at = now()
WHERE id = $1
RETURNING *;

-- name: InsertRescheduleAudit :exec
INSERT INTO action_item_reschedules
    (id, action_item_id, from_version, to_version, old_due_at, new_due_at, actor_id, request_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8);

-- Open, due items that have never had a dispatch for their CURRENT version.
-- The LEFT JOIN + NULL check is what stops an old schedule version from ever
-- reminding after a reschedule.
-- name: DueActionItems :many
SELECT ai.*
FROM action_items ai
WHERE ai.status = 'open'
  AND ai.due_at <= $1
  AND NOT EXISTS (
      SELECT 1 FROM reminder_dispatches rd
      WHERE rd.action_item_id = ai.id AND rd.version = ai.version
  )
ORDER BY ai.due_at
LIMIT $2;

-- name: MarkReminderDispatched :execrows
INSERT INTO reminder_dispatches (action_item_id, version)
VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: CreateNotification :one
INSERT INTO notifications (id, action_item_id, owner_id, message, due_at, version)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *;

-- name: ListNotifications :many
SELECT * FROM notifications
WHERE (@include_all::boolean = true OR owner_id = $1)
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;
