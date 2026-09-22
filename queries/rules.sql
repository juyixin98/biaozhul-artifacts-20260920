-- name: MaxRuleVersion :one
SELECT COALESCE(MAX(version), 0)::INTEGER AS version FROM rule_versions;

-- name: CreateRuleVersion :one
INSERT INTO rule_versions (version, note) VALUES ($1, $2) RETURNING *;

-- name: AddSensitiveWord :exec
INSERT INTO sensitive_words (rule_version_id, word)
VALUES ($1, $2)
ON CONFLICT DO NOTHING;

-- name: ActivateRule :exec
UPDATE rule_versions SET active = true, activated_at = now() WHERE id = $1;

-- name: DeactivateActiveRules :exec
UPDATE rule_versions SET active = false, activated_at = NULL WHERE active;

-- name: GetRuleVersion :one
SELECT * FROM rule_versions WHERE id = $1;

-- name: GetActiveRule :one
SELECT * FROM rule_versions WHERE active = true ORDER BY version DESC LIMIT 1;

-- name: ListRuleWords :many
SELECT word FROM sensitive_words WHERE rule_version_id = $1 ORDER BY word;

-- name: ListRuleVersions :many
SELECT * FROM rule_versions ORDER BY version DESC;

-- Transaction-scoped advisory lock. Publishing and rule activation both take
-- this same lock, so a publish racing a rule switch has a deterministic
-- winner rule version: the one active when the lock is acquired.
-- name: LockRuleAdvisory :exec
SELECT pg_advisory_xact_lock(91729329);
