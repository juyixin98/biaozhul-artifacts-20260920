-- Advisory lock used to SERIALIZE rule activation against moderation decisions.
-- Both code paths take this same transaction-scoped lock first, so a decision can
-- never run concurrently with a rule switch; the outcome is deterministic.
-- name: TakeRuleLock :exec
SELECT pg_advisory_xact_lock($1);

-- name: GetActiveRule :one
SELECT * FROM rule_versions WHERE status = 'active';

-- name: GetActiveRuleForUpdate :one
SELECT * FROM rule_versions WHERE status = 'active' FOR UPDATE;

-- name: GetRuleVersion :one
SELECT * FROM rule_versions WHERE id = $1;

-- name: ArchiveActiveRule :exec
UPDATE rule_versions SET status = 'archived' WHERE status = 'active';

-- name: NextRuleVersionNo :one
SELECT COALESCE(MAX(version), 0)::bigint AS next_no FROM rule_versions;

-- name: InsertRuleVersion :one
INSERT INTO rule_versions (version, status, description, created_by)
VALUES ($1, 'active', $2, $3)
RETURNING *;

-- name: InsertRuleWord :exec
INSERT INTO rule_words (rule_version_id, word) VALUES ($1, $2);

-- name: ListRuleWords :many
SELECT word FROM rule_words WHERE rule_version_id = $1;

-- name: ListRuleVersions :many
SELECT * FROM rule_versions ORDER BY version DESC;
