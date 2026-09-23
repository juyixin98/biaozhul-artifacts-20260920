-- name: ListOrganizations :many
SELECT * FROM organizations ORDER BY code;

-- name: ListCostCentersScoped :many
SELECT cc.* FROM cost_centers cc
WHERE cc.org_id = ANY($1::uuid[])
ORDER BY cc.org_id, cc.code;

-- name: GetCostCenterScoped :one
SELECT cc.* FROM cost_centers cc
WHERE cc.id = $1 AND cc.org_id = ANY($2::uuid[]);

-- name: ListAccountsScoped :many
SELECT a.* FROM accounts a
WHERE a.org_id = ANY($1::uuid[])
ORDER BY a.org_id, a.code;

-- name: GetAccountByCodeScoped :one
SELECT a.* FROM accounts a
WHERE a.org_id = ANY($1::uuid[]) AND a.code = $2;

-- name: GetAccountScoped :one
SELECT a.* FROM accounts a
WHERE a.id = $1 AND a.org_id = ANY($2::uuid[]);

-- name: GetResourceByCode :one
SELECT * FROM resources WHERE code = $1;

-- name: UpsertResource :one
INSERT INTO resources (code, service) VALUES ($1, $2)
ON CONFLICT (code) DO UPDATE SET service = resources.service -- never reassign
RETURNING *;

-- name: GetResourcesByCodes :many
SELECT * FROM resources WHERE code = ANY($1::text[]);

-- name: GetAccountsByCodesScoped :many
SELECT a.* FROM accounts a
WHERE a.org_id = ANY($1::uuid[]) AND a.code = ANY($2::text[]);

-- name: AllAccountIDs :many
SELECT id FROM accounts ORDER BY id;

-- name: GetResourcesByIDs :many
SELECT * FROM resources WHERE id = ANY($1::uuid[]);
