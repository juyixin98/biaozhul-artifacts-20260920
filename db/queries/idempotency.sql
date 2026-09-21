-- name: InsertIdempotency :one
INSERT INTO idempotency_keys
    (key, merchant_id, endpoint, request_hash, resource_id, response_status, response_body)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING *;

-- name: GetIdempotency :one
SELECT * FROM idempotency_keys
WHERE key = $1 AND merchant_id = $2 AND endpoint = $3
FOR UPDATE;

-- name: GetIdempotencyByKeyEndpoint :one
SELECT * FROM idempotency_keys
WHERE key = $1 AND endpoint = $2;
