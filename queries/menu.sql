-- name: GetLiveMenu :many
-- One consistent snapshot of the live menu: effective price (active temp
-- price band wins, half-open [starts_at, ends_at)) and derived sold-out flag
-- from today's sales in the store timezone. A single query = a single
-- database snapshot, so a response can never mix old and new state.
SELECT
    mv.version,
    vi.item_key,
    vi.name,
    vi.position,
    vi.price_cents AS base_price_cents,
    COALESCE(tp.price_cents, vi.price_cents) AS effective_price_cents,
    (tp.id IS NOT NULL)::bool AS temp_price_active,
    vi.sold_out_threshold,
    COALESCE(ds.quantity, 0) AS sold_today,
    (vi.sold_out_threshold > 0 AND COALESCE(ds.quantity, 0) >= vi.sold_out_threshold)::bool AS sold_out
FROM stores s
JOIN menu_versions mv ON mv.store_id = s.id AND mv.version = s.current_version
JOIN version_items vi ON vi.version_id = mv.id
LEFT JOIN version_temp_prices tp
       ON tp.version_id = vi.version_id AND tp.item_key = vi.item_key
      AND tp.starts_at <= now() AND now() < tp.ends_at
LEFT JOIN daily_sales ds
       ON ds.store_id = s.id AND ds.item_key = vi.item_key
      AND ds.day = (now() AT TIME ZONE s.timezone)::date
WHERE s.id = $1
ORDER BY vi.position, vi.item_key;

-- name: ListVersionTempPrices :many
SELECT id, version_id, item_key, price_cents, starts_at, ends_at FROM version_temp_prices WHERE version_id = $1 ORDER BY item_key, starts_at;
