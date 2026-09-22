-- name: CreateTier :one
INSERT INTO tiers (community_id, level, name, price_cents, duration_days)
VALUES ($1, $2, $3, $4, $5) RETURNING *;

-- name: GetTier :one
SELECT * FROM tiers WHERE id = $1 AND community_id = $2;

-- name: ListTiers :many
SELECT * FROM tiers WHERE community_id = $1 ORDER BY level;

-- name: SetTierActive :exec
UPDATE tiers SET is_active = $3 WHERE id = $1 AND community_id = $2;

-- name: CountTiers :one
SELECT count(*) FROM tiers WHERE community_id = $1;

-- Returns the member's effective tier level, or sqlc.ErrNoRows when there is no
-- active, unexpired subscription. Single locking read so renewal and access
-- checks serialize on the subscription row.
-- name: GetEffectiveLevel :one
SELECT t.level
FROM subscriptions s
JOIN tiers t ON t.id = s.tier_id
WHERE s.community_id = $1 AND s.user_id = $2
  AND s.status = 'active' AND s.period_end > now()
  AND t.is_active = TRUE
FOR SHARE OF s;

-- name: GetSubscription :one
SELECT * FROM subscriptions
WHERE community_id = $1 AND user_id = $2;

-- Single-statement insert-or-renew. On the unique (community_id,user_id)
-- conflict PostgreSQL takes a row lock and re-evaluates the DO UPDATE against
-- the winner's committed row, so two first-time payments that race serialize
-- into one insert plus one renewal instead of a duplicate key error.
-- name: UpsertSubscription :one
INSERT INTO subscriptions (community_id, user_id, tier_id, status, period_end)
VALUES ($1, $2, $3, 'active', now() + make_interval(days => $4))
ON CONFLICT (community_id, user_id) DO UPDATE
SET tier_id    = EXCLUDED.tier_id,
    status     = 'active',
    period_end = GREATEST(subscriptions.period_end, now())
                 + make_interval(days => $4)
RETURNING *;

-- Immediate loss of access: status flips to cancelled; access checks require
-- status='active', so the change takes effect at once.
-- name: CancelSubscription :exec
UPDATE subscriptions SET status = 'cancelled'
WHERE community_id = $1 AND user_id = $2 AND status = 'active';

-- Idempotency lookup for payment registration.
-- name: GetPaymentByRequest :one
SELECT * FROM payments WHERE community_id = $1 AND request_id = $2;

-- name: CreatePayment :one
INSERT INTO payments (community_id, request_id, user_id, tier_id, amount_cents, days)
VALUES ($1, $2, $3, $4, $5, $6) RETURNING *;
