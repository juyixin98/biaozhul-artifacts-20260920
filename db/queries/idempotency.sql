-- name: GetTransitionRequest :one
SELECT * FROM transition_requests WHERE request_id = $1;

-- name: InsertTransitionRequest :exec
INSERT INTO transition_requests (request_id, incident_id, response)
VALUES ($1, $2, $3);
