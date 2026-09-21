-- name: CreateActionItem :one
INSERT INTO action_items (incident_id, title, owner_id, due_at)
VALUES ($1, $2, $3, $4)
RETURNING *;

-- name: GetActionItemForUpdate :one
SELECT * FROM action_items WHERE id = $1 FOR UPDATE;

-- name: ListActionItems :many
SELECT * FROM action_items WHERE incident_id = $1 ORDER BY created_at, id;

-- name: CountActionItems :one
SELECT count(*) FROM action_items WHERE incident_id = $1;

-- name: RescheduleActionItem :one
UPDATE action_items
SET due_at = $2, due_version = due_version + 1, updated_at = now()
WHERE id = $1
RETURNING *;

-- name: ListDueActionItems :many
SELECT * FROM action_items
WHERE status = 'open'
  AND due_at <= now()
  AND NOT EXISTS (
      SELECT 1 FROM reminders r
      WHERE r.action_item_id = action_items.id
        AND r.due_version = action_items.due_version
  )
ORDER BY due_at
LIMIT $1;

-- name: ReminderExists :one
SELECT EXISTS(
    SELECT 1 FROM reminders
    WHERE action_item_id = $1 AND due_version = $2
);

-- name: InsertReminder :exec
INSERT INTO reminders (action_item_id, due_version, due_at)
VALUES ($1, $2, $3);

-- name: ListRemindersForIncident :many
SELECT r.* FROM reminders r
JOIN action_items a ON a.id = r.action_item_id
WHERE a.incident_id = $1
ORDER BY r.id;
