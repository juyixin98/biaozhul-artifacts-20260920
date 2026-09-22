-- name: AllocateRuleID :one
SELECT nextval('rules_id_seq')::bigint AS id;

-- name: InsertRule :one
INSERT INTO rules (
    id, org_id, version, kind, name, enabled,
    window_seconds, max_events, tables, hour_start, hour_end, config_json, created_by
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10, $11, $12, $13
)
RETURNING *;

-- name: NextRuleVersion :one
SELECT COALESCE(MAX(version), 0) + 1 AS next_version
FROM rules WHERE org_id = $1 AND id = $2;

-- name: GetRuleVersion :one
SELECT * FROM rules WHERE org_id = $1 AND id = $2 AND version = $3;

-- name: GetCurrentRules :many
SELECT DISTINCT ON (id) *
FROM rules
WHERE org_id = $1 AND enabled = TRUE
ORDER BY id, version DESC;

-- name: ListRuleVersions :many
SELECT * FROM rules WHERE org_id = $1 AND id = $2 ORDER BY version DESC;

-- name: ListRules :many
SELECT DISTINCT ON (id) *
FROM rules
WHERE org_id = $1
ORDER BY id, version DESC;

-- name: InsertEvent :one
INSERT INTO events (
    org_id, source_id, source_key, event_id, db_user, occurred_at,
    action, schema_name, table_name, row_count, content_hash
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10, $11
)
RETURNING id, org_id, source_id, source_key, event_id, db_user, occurred_at,
          action, schema_name, table_name, row_count, content_hash, received_at;

-- name: GetEventBySourceEvent :one
SELECT id, org_id, source_id, source_key, event_id, db_user, occurred_at,
       action, schema_name, table_name, row_count, content_hash, received_at
FROM events
WHERE source_id = $1 AND event_id = $2;

-- Events of one source occurring inside a half-open time window [after, before).
-- name: EventsInWindow :many
SELECT id, org_id, source_id, source_key, event_id, db_user, occurred_at,
       action, schema_name, table_name, row_count, content_hash, received_at
FROM events
WHERE org_id = $1
  AND db_user = $2
  AND occurred_at >= $3
  AND occurred_at <  $4
ORDER BY occurred_at, id;

-- name: GetEventByID :one
SELECT id, org_id, source_id, source_key, event_id, db_user, occurred_at,
       action, schema_name, table_name, row_count, content_hash, received_at
FROM events WHERE org_id = $1 AND id = $2;

-- name: ListEvents :many
SELECT id, org_id, source_id, source_key, event_id, db_user, occurred_at,
       action, schema_name, table_name, row_count, content_hash, received_at
FROM events
WHERE org_id = $1
  AND (sqlc.narg('from_time')::timestamptz IS NULL OR occurred_at >= sqlc.narg('from_time'))
  AND (sqlc.narg('to_time')::timestamptz IS NULL OR occurred_at <  sqlc.narg('to_time'))
ORDER BY occurred_at DESC, id DESC
LIMIT $2;

-- Keyset page for export: rows strictly before the (cursor_time, cursor_id)
-- tuple, descending. First page passes cursor_id = 0.
-- name: ExportEventsPage :many
SELECT id, org_id, source_id, source_key, event_id, db_user, occurred_at,
       action, schema_name, table_name, row_count, content_hash, received_at
FROM events
WHERE org_id = $1
  AND ($2::timestamptz IS NULL OR occurred_at >= $2)
  AND ($3::timestamptz IS NULL OR occurred_at <  $3)
  AND ($4::bigint = 0
       OR (occurred_at, id) < ($5::timestamptz, $6::bigint))
ORDER BY occurred_at DESC, id DESC
LIMIT $7;

