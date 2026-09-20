-- Reserve a request id. If it already exists the caller replays the stored
-- response instead of executing the operation again.
-- name: InsertIdempotentRequest :execrows
INSERT INTO idempotent_requests (request_id, actor_id, method, path, response, status_code)
VALUES ($1, $2, $3, $4, '{}'::jsonb, 0);

-- name: GetIdempotentRequest :one
SELECT * FROM idempotent_requests WHERE request_id = $1;

-- name: CompleteIdempotentRequest :exec
UPDATE idempotent_requests
SET response = @response::jsonb, status_code = @status_code
WHERE request_id = @request_id AND status_code = 0;

-- name: InsertAuditEvent :exec
INSERT INTO audit_events (id, incident_id, actor_id, action, detail)
VALUES (@id, @incident_id, @actor_id, @action, @detail::jsonb);

-- name: ListAuditEvents :many
SELECT * FROM audit_events
WHERE (@incident_filter::boolean = false OR incident_id = $1)
ORDER BY created_at DESC
LIMIT $2 OFFSET $3;
