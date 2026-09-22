-- name: TakeAuditLock :exec
SELECT pg_advisory_xact_lock($1::int, $2::int);

-- name: MaxAuditSeq :one
SELECT COALESCE(MAX(seq), 0)::bigint AS max_seq FROM audit_entries WHERE org_id = $1;

-- name: GetAuditEntry :one
SELECT org_id, seq, actor_id, action, content, content_hash,
       prev_hash, entry_hash, created_at
FROM audit_entries WHERE org_id = $1 AND seq = $2;

-- name: InsertAuditEntry :exec
INSERT INTO audit_entries (
    org_id, seq, actor_id, action, content, content_hash, prev_hash, entry_hash
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8);

-- name: ListAuditEntries :many
SELECT org_id, seq, actor_id, action, content, content_hash,
       prev_hash, entry_hash, created_at
FROM audit_entries
WHERE org_id = $1 AND seq > $2
ORDER BY seq
LIMIT $3;

-- Full chain in order, for verification.
-- name: AllAuditEntries :many
SELECT org_id, seq, actor_id, action, content, content_hash,
       prev_hash, entry_hash, created_at
FROM audit_entries WHERE org_id = $1
ORDER BY seq;

-- name: CountAuditEntries :one
SELECT count(*) AS cnt, COALESCE(MAX(seq), 0)::bigint AS max_seq
FROM audit_entries WHERE org_id = $1;
