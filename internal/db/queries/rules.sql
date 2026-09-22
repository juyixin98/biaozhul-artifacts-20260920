-- name: GetRuleVersion :one
SELECT * FROM rule_versions WHERE id = $1;

-- name: ListRuleVersions :many
SELECT * FROM rule_versions WHERE org_id = $1 AND rule_type = $2 ORDER BY version DESC;

-- name: GetActiveRuleVersions :many
SELECT * FROM rule_versions
WHERE org_id = $1 AND is_active = TRUE ORDER BY rule_type;

-- Rule version in effect for a given window start: the highest version whose
-- effective_at <= window_start (still active). If the effective version was
-- explicitly superseded (is_active=false), the next one applies.
-- name: GetRuleVersionAt :one
SELECT * FROM rule_versions
WHERE org_id = $1 AND rule_type = $2 AND effective_at <= $3
ORDER BY effective_at DESC, version DESC
LIMIT 1;

-- name: CreateRuleVersion :one
INSERT INTO rule_versions (org_id, rule_type, version, is_active, params, effective_at, created_by)
VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *;

-- name: DeactivateRuleVersions :exec
UPDATE rule_versions SET is_active = FALSE
WHERE org_id = $1 AND rule_type = $2 AND id <> $3;

-- Deactivate every active version of a type BEFORE inserting the new one, so
-- the partial unique index (one active per org+type) never blocks the insert.
-- name: DeactivateAllActiveRules :exec
UPDATE rule_versions SET is_active = FALSE
WHERE org_id = $1 AND rule_type = $2 AND is_active = TRUE;

-- name: NextRuleVersion :one
SELECT COALESCE(max(version), 0) + 1 AS next_version
FROM rule_versions WHERE org_id = $1 AND rule_type = $2;

-- name: UpsertDetectionWindow :one
INSERT INTO detection_windows (
    org_id, rule_version_id, db_user, window_start, window_end,
    event_count, first_event_at, last_event_at, updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
ON CONFLICT (org_id, rule_version_id, db_user, window_start)
DO UPDATE SET event_count = EXCLUDED.event_count,
              first_event_at = EXCLUDED.first_event_at,
              last_event_at = EXCLUDED.last_event_at,
              updated_at = now()
RETURNING *;

-- name: GetDetectionWindow :one
SELECT * FROM detection_windows
WHERE org_id = $1 AND rule_version_id = $2 AND db_user = $3 AND window_start = $4;
