-- name: AuthByAPIKeyHash :one
SELECT u.id, u.login, u.full_name, u.role,
       (SELECT COALESCE(array_agg(us.org_id ORDER BY us.org_id), '{}')
          FROM user_org_scopes us WHERE us.user_id = u.id)::uuid[] AS org_ids
FROM api_keys k
JOIN app_users u ON u.id = k.user_id
WHERE k.key_hash = $1 AND k.revoked_at IS NULL;

-- name: CreateImport :one
INSERT INTO cost_imports
    (org_id, user_id, filename, status, raw_row_count, row_count, error_line, error_code, error_message)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
RETURNING *;

-- name: SetImportSucceeded :exec
UPDATE cost_imports
SET status = 'succeeded', row_count = $2,
    error_line = NULL, error_code = NULL, error_message = NULL
WHERE id = $1;

-- name: GetImportScoped :one
SELECT ci.* FROM cost_imports ci
WHERE ci.id = $1 AND ci.org_id = ANY($2::uuid[]);

-- name: ListImportsScoped :many
SELECT ci.* FROM cost_imports ci
WHERE ci.org_id = ANY($1::uuid[])
ORDER BY ci.created_at DESC, ci.id DESC
LIMIT $2 OFFSET $3;

-- name: ListAllImports :many
SELECT * FROM cost_imports
ORDER BY created_at DESC, id DESC
LIMIT $1 OFFSET $2;

-- name: CountImportsScoped :one
SELECT count(*) FROM cost_imports ci WHERE ci.org_id = ANY($1::uuid[]);

-- name: CountAllImports :one
SELECT count(*) FROM cost_imports;
