-- name: InsertAuditLog :exec
INSERT INTO audit_logs (actor_user, actor_role, merchant_id, action, target_type, target_id, masked, detail, ip)
VALUES (sqlc.arg(actor_user), sqlc.arg(actor_role), sqlc.arg(merchant_id),
        sqlc.arg(action), sqlc.arg(target_type), sqlc.arg(target_id),
        sqlc.arg(masked), sqlc.arg(detail), sqlc.arg(ip));

-- name: ListAuditLogs :many
SELECT * FROM audit_logs
WHERE (sqlc.narg(merchant_id)::uuid IS NULL OR merchant_id = sqlc.narg(merchant_id)::uuid)
  AND (sqlc.narg(action_filter)::text IS NULL OR action = sqlc.narg(action_filter)::text)
ORDER BY created_at DESC
LIMIT sqlc.arg(row_limit) OFFSET sqlc.arg(row_offset);

-- name: ListAuditLogsForMerchant :many
SELECT * FROM audit_logs WHERE merchant_id = sqlc.arg(merchant_id)
ORDER BY created_at DESC LIMIT sqlc.arg(row_limit) OFFSET sqlc.arg(row_offset);
