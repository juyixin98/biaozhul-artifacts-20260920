-- name: CreateOrg :one
INSERT INTO organizations (external_id, name)
VALUES ($1, $2)
RETURNING id, external_id, name, created_at;

-- name: GetOrgByExternalID :one
SELECT id, external_id, name, created_at FROM organizations WHERE external_id = $1;

-- name: GetOrgByID :one
SELECT id, external_id, name, created_at FROM organizations WHERE id = $1;

-- name: ListOrgs :many
SELECT id, external_id, name, created_at FROM organizations ORDER BY id;

-- name: CreateCostCenter :one
INSERT INTO cost_centers (org_id, code, name)
VALUES ($1, $2, $3)
RETURNING id, org_id, code, name, created_at;

-- name: GetCostCenterByCode :one
SELECT id, org_id, code, name, created_at FROM cost_centers WHERE org_id = $1 AND code = $2;

-- name: GetCostCenterByID :one
SELECT id, org_id, code, name, created_at FROM cost_centers WHERE id = $1;

-- name: ListCostCenters :many
SELECT id, org_id, code, name, created_at
FROM cost_centers
WHERE (cardinality(COALESCE(@org_ids, ARRAY[]::bigint[])) = 0 OR org_id = ANY(@org_ids))
ORDER BY id;

-- name: CreateAccount :one
INSERT INTO accounts (org_id, cost_center_id, external_id, name)
VALUES ($1, $2, $3, $4)
RETURNING id, org_id, cost_center_id, external_id, name, created_at;

-- name: GetAccountByExternalID :one
SELECT id, org_id, cost_center_id, external_id, name, created_at
FROM accounts WHERE external_id = $1;

-- name: ListAccounts :many
SELECT id, org_id, cost_center_id, external_id, name, created_at
FROM accounts
WHERE (cardinality(COALESCE(@org_ids, ARRAY[]::bigint[])) = 0 OR org_id = ANY(@org_ids))
ORDER BY id;
