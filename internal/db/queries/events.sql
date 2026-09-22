-- Insert returns the new row; on conflict (duplicate replay or a concurrent
-- transaction winning the race) sql.ErrNoRows is returned instead, and callers
-- re-read with GetExistingEvents to distinguish replay vs conflict.
-- name: InsertEvent :one
INSERT INTO events (
    org_id, source_id, source_event_id, db_user, occurred_at,
    action_category, schema_name, table_name, row_count, client_ip,
    sql_text, content_hash, batch_id
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)
ON CONFLICT (org_id, source_id, source_event_id) DO NOTHING
RETURNING *;

-- name: GetExistingEvents :many
SELECT source_event_id, content_hash
FROM events
WHERE org_id = $1 AND source_id = $2 AND source_event_id = ANY(sqlc.arg(event_ids)::text[]);

-- name: GetEventByID :one
SELECT * FROM events WHERE org_id = $1 AND id = $2;

-- IDs of events belonging to a frequency window.
-- name: EventIDsInWindow :many
SELECT id FROM events
WHERE org_id = $1 AND db_user = $2
  AND occurred_at >= sqlc.arg(window_start) AND occurred_at < sqlc.arg(window_end)
  AND (sqlc.arg(actions)::text[] IS NULL OR action_category = ANY(sqlc.arg(actions)::text[]))
ORDER BY id;

-- name: CreateIngestBatch :one
INSERT INTO ingest_batches (id, org_id, source_id, received_count, inserted_count, duplicate_count, status)
VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *;

-- name: ListEventsByOrg :many
SELECT * FROM events
WHERE org_id = $1
  AND (sqlc.narg(from_time)::timestamptz IS NULL OR occurred_at >= sqlc.narg(from_time))
  AND (sqlc.narg(to_time)::timestamptz IS NULL OR occurred_at <  sqlc.narg(to_time))
  AND (sqlc.narg(filter_user)::text IS NULL OR db_user = sqlc.narg(filter_user))
  AND (sqlc.narg(filter_table)::text IS NULL OR table_name = sqlc.narg(filter_table))
ORDER BY occurred_at, id
LIMIT sqlc.arg(row_limit) OFFSET sqlc.arg(row_offset);

-- name: CountEventsByOrg :one
SELECT count(*) FROM events
WHERE org_id = $1
  AND (sqlc.narg(from_time)::timestamptz IS NULL OR occurred_at >= sqlc.narg(from_time))
  AND (sqlc.narg(to_time)::timestamptz IS NULL OR occurred_at <  sqlc.narg(to_time));

-- Authoritative recomputation of a stepped frequency window straight from
-- the events table. Boundaries are [window_start, window_end) — half open.
-- name: CountEventsInWindow :one
SELECT count(*)::int AS event_count,
       min(occurred_at)::timestamptz AS first_at,
       max(occurred_at)::timestamptz AS last_at
FROM events
WHERE org_id = $1
  AND db_user = $2
  AND occurred_at >= sqlc.arg(window_start)
  AND occurred_at <  sqlc.arg(window_end)
  AND (sqlc.arg(actions)::text[] IS NULL OR action_category = ANY(sqlc.arg(actions)::text[]));
