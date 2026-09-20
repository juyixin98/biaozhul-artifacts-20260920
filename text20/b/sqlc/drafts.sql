-- name: GetOrCreateDraft :one
INSERT INTO menu_drafts (store_id) VALUES ($1)
ON CONFLICT (store_id) DO UPDATE SET updated_at = now()
RETURNING id, store_id, version, updated_at;

-- name: GetDraft :one
SELECT id, store_id, version, updated_at FROM menu_drafts
WHERE store_id = $1;

-- name: BumpDraftVersion :exec
UPDATE menu_drafts SET version = version + 1, updated_at = now()
WHERE store_id = $1;

-- name: ClearDraftItems :exec
DELETE FROM draft_items WHERE draft_id = $1;

-- name: InsertDraftItem :one
INSERT INTO draft_items (draft_id, dish_id, position, price)
VALUES ($1, $2, $3, $4)
RETURNING id, draft_id, dish_id, position, price;

-- name: ListDraftItems :many
SELECT di.id, di.draft_id, di.dish_id, di.position, di.price,
       d.name AS dish_name, d.base_price
FROM draft_items di
JOIN dishes d ON d.id = di.dish_id
WHERE di.draft_id = $1
ORDER BY di.position;
