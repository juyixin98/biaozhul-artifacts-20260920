-- name: InsertTemporaryPrice :exec
INSERT INTO temporary_prices (store_id, version_id, sku, price, start_at, end_at)
VALUES ($1, $2, $3, $4, $5, $6);

-- Temporary price in effect at a fixed instant. Windows are half-open and
-- non-overlapping, so at most one row matches per (version, sku).
-- name: ListActiveTempPrices :many
SELECT id, version_id, sku, price, start_at, end_at
FROM temporary_prices
WHERE version_id = $1
  AND start_at <= $2
  AND end_at > $2;

-- name: ListTempPricesForVersion :many
SELECT * FROM temporary_prices
WHERE version_id = $1
ORDER BY sku, start_at;
