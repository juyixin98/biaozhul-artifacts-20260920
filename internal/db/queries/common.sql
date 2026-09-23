-- name: AcquireTxAdvisoryLock :exec
SELECT pg_advisory_xact_lock($1);

-- name: Now :one
SELECT now();
