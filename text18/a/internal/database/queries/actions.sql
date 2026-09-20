-- name: CreateActionItem :one
INSERT INTO action_items (incident_id, description, owner_user_id, due_at, created_by)
VALUES ($1, $2, $3, $4, $5)
RETURNING *;

-- name: GetActionItem :one
SELECT * FROM action_items
WHERE id = $1 AND incident_id = $2;

-- name: GetActionItemForUpdate :one
SELECT * FROM action_items
WHERE id = $1 AND incident_id = $2
FOR UPDATE;

-- name: CountActionItems :one
SELECT count(*)::bigint AS item_count
FROM action_items
WHERE incident_id = $1;

-- Effective items for the closure gate: every such row carries a non-null
-- owner and deadline by construction; canceled items do not satisfy it.
-- name: CountEffectiveActionItems :one
SELECT count(*)::bigint AS item_count
FROM action_items
WHERE incident_id = $1 AND status IN ('open', 'done');

-- name: ListActionItemsByIncident :many
SELECT a.*, u.username AS owner_username, u.full_name AS owner_full_name
FROM action_items a
JOIN users u ON u.id = a.owner_user_id
WHERE a.incident_id = $1
ORDER BY a.due_at, a.id;

-- Rescheduling atomically replaces the schedule and bumps due_version.
-- Only the currently stored (version, due_at) pair can ever be reminded.
-- name: RescheduleActionItem :one
UPDATE action_items
SET due_at = $3,
    due_version = due_version + 1,
    updated_at = now()
WHERE id = $1 AND incident_id = $2 AND status = 'open'
RETURNING *;

-- name: SetActionItemStatus :one
UPDATE action_items
SET status = $3,
    updated_at = now()
WHERE id = $1 AND incident_id = $2 AND status = 'open'
RETURNING *;

-- Due items are claimed with SKIP LOCKED: concurrent sweeps (or a sweep
-- racing a reschedule) never act on the same row twice. Rows whose due time
-- has just been pushed into the future are not visible to this predicate.
-- name: DueActionItems :many
SELECT * FROM action_items
WHERE status = 'open' AND due_at <= $1
ORDER BY due_at, id
LIMIT $2
FOR UPDATE SKIP LOCKED;

-- name: CreateReminder :one
INSERT INTO reminders (action_item_id, incident_id, due_version, due_at, message)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (action_item_id, due_version) DO NOTHING
RETURNING *;

-- name: ListRemindersByIncident :many
SELECT * FROM reminders
WHERE incident_id = $1
ORDER BY created_at, id;

-- name: CountRemindersForVersion :one
SELECT count(*)::bigint AS reminder_count
FROM reminders
WHERE action_item_id = $1 AND due_version = $2;
