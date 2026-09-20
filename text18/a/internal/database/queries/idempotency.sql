-- Transaction-scoped advisory lock used to serialize concurrent
-- idempotency replays for the same logical request.
-- name: AdvisoryXactLock :exec
SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0));

-- name: GetIdempotentRequest :one
SELECT * FROM idempotent_requests
WHERE request_id = $1;

-- name: CreateIdempotentRequest :one
INSERT INTO idempotent_requests
    (request_id, user_id, incident_id, method, path, status_code, response_body)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (request_id) DO NOTHING
RETURNING *;
