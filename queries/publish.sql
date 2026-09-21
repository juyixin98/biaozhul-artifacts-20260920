-- name: BumpStoreVersion :one
-- Atomically claim the next version. Zero rows updated => expected_version
-- did not match => exactly one concurrent publisher wins.
UPDATE stores SET current_version = current_version + 1
WHERE id = $1 AND current_version = $2
RETURNING current_version;

-- name: CreateMenuVersion :one
INSERT INTO menu_versions (store_id, version) VALUES ($1, $2) RETURNING id, store_id, version, published_at;

-- name: CopyDraftItemsToVersion :exec
INSERT INTO version_items (version_id, item_key, name, price_cents, sold_out_threshold, position)
SELECT $1, item_key, name, price_cents, sold_out_threshold, position
FROM draft_items WHERE store_id = $2;

-- name: CopyDraftTempPricesToVersion :exec
INSERT INTO version_temp_prices (version_id, item_key, price_cents, starts_at, ends_at)
SELECT $1, item_key, price_cents, starts_at, ends_at
FROM draft_temp_prices WHERE store_id = $2;
