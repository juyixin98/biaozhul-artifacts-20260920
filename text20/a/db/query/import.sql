-- Batch import upsert keyed on (store_id, sku). Used inside one transaction by
-- the import handler, which validates the whole batch first; any failure rolls
-- the entire import back (all-or-nothing, max 500 items).
-- name: UpsertDish :one
INSERT INTO dishes (store_id, sku, name, base_price, active, daily_limit, sort_order)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (store_id, sku) DO UPDATE
SET name = EXCLUDED.name,
    base_price = EXCLUDED.base_price,
    active = EXCLUDED.active,
    daily_limit = EXCLUDED.daily_limit,
    sort_order = EXCLUDED.sort_order,
    updated_at = now()
RETURNING *;

-- name: DeactivateDishBySku :exec
UPDATE dishes SET active = FALSE, updated_at = now()
WHERE store_id = $1 AND sku = $2;
