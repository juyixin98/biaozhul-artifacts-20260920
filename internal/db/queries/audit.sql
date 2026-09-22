-- name: LockAuditChain :one
INSERT INTO audit_chain_state (org_id) VALUES ($1)
ON CONFLICT (org_id) DO UPDATE SET org_id = EXCLUDED.org_id
RETURNING head_seq, head_hash;

-- name: AdvanceAuditChain :exec
UPDATE audit_chain_state
SET head_seq = $2, head_hash = $3, updated_at = now()
WHERE org_id = $1;

-- name: InsertAuditEntry :one
INSERT INTO audit_entries (
    org_id, seq, entry_type, actor_api_key_id, actor_label,
    payload, prev_hash, entry_hash
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *;

-- name: ListAuditEntries :many
SELECT * FROM audit_entries
WHERE org_id = $1
  AND (sqlc.narg(from_seq)::bigint IS NULL OR seq >= sqlc.narg(from_seq))
  AND (sqlc.narg(to_seq)::bigint IS NULL OR seq <= sqlc.narg(to_seq))
ORDER BY seq
LIMIT $2;

-- name: GetAuditEntry :one
SELECT * FROM audit_entries WHERE org_id = $1 AND seq = $2;

-- name: AuditChainTip :one
SELECT head_seq, head_hash FROM audit_chain_state WHERE org_id = $1;

-- name: CountAuditEntries :one
SELECT count(*) FROM audit_entries WHERE org_id = $1;

-- name: MaxAuditSeq :one
SELECT COALESCE(max(seq), 0)::bigint AS max_seq FROM audit_entries WHERE org_id = $1;
