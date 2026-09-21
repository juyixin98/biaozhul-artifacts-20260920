-- name: UpsertDraftItem :one
INSERT INTO draft_items (store_id, item_key, name, price_cents, sold_out_threshold, position)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (store_id, item_key) DO UPDATE SET
    name = EXCLUDED.name,
    price_cents = EXCLUDED.price_cents,
    sold_out_threshold = EXCLUDED.sold_out_threshold,
    position = EXCLUDED.position
RETURNING store_id, item_key, name, price_cents, sold_out_threshold, position;

-- name: GetDraftItem :one
SELECT store_id, item_key, name, price_cents, sold_out_threshold, position FROM draft_items WHERE store_id = $1 AND item_key = $2;

-- name: ListDraftItems :many
SELECT store_id, item_key, name, price_cents, sold_out_threshold, position FROM draft_items WHERE store_id = $1 ORDER BY position, item_key;

-- name: DeleteDraftItem :execrows
DELETE FROM draft_items WHERE store_id = $1 AND item_key = $2;

-- name: CreateDraftTempPrice :one
INSERT INTO draft_temp_prices (store_id, item_key, price_cents, starts_at, ends_at)
VALUES ($1, $2, $3, $4, $5) RETURNING id, store_id, item_key, price_cents, starts_at, ends_at;

-- name: ListDraftTempPrices :many
SELECT id, store_id, item_key, price_cents, starts_at, ends_at FROM draft_temp_prices WHERE store_id = $1 ORDER BY item_key, starts_at;

-- name: DeleteDraftTempPrice :execrows
DELETE FROM draft_temp_prices WHERE store_id = $1 AND id = $2;
