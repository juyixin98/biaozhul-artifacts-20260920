-- name: CreateTier :one
INSERT INTO tiers (community_id, level, name, price_cents, duration_days)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, community_id, level, name, price_cents, duration_days;

-- name: GetTier :one
SELECT id, community_id, level, name, price_cents, duration_days
FROM tiers WHERE id = $1;

-- name: ListTiers :many
SELECT id, community_id, level, name, price_cents, duration_days
FROM tiers WHERE community_id = $1 ORDER BY level;

-- name: CountTiers :one
SELECT count(*) AS cnt FROM tiers WHERE community_id = $1;

-- name: GetMembershipForUpdate :one
SELECT * FROM memberships
WHERE community_id = $1 AND user_id = $2
FOR UPDATE;

-- name: CreateMembershipIfMissing :one
-- New membership valid from now() until now()+duration (database clock).
-- ON CONFLICT DO NOTHING makes first-time concurrent renewals safe: exactly one
-- transaction inserts; the loser re-reads the row (the caller then locks it).
INSERT INTO memberships (community_id, user_id, tier_id, starts_at, expires_at)
SELECT $1, $2, $3, now(), now() + ($4::int * INTERVAL '1 day')
ON CONFLICT (community_id, user_id) DO NOTHING
RETURNING *;

-- name: ExtendMembership :one
-- Append days from the later of now() and the current expiry, so no time is
-- lost under concurrent renewals (this runs inside a row-locked transaction).
UPDATE memberships
SET tier_id    = $3,
    expires_at = GREATEST(expires_at, now()) + ($4::int * INTERVAL '1 day'),
    status     = 'active'
WHERE id = $1 AND community_id = $2
RETURNING *;

-- name: CancelMembership :one
UPDATE memberships SET status = 'cancelled'
WHERE community_id = $1 AND user_id = $2
RETURNING *;

-- name: GetMembership :one
SELECT * FROM memberships WHERE community_id = $1 AND user_id = $2;

-- name: FindPaymentByRequestID :one
SELECT id, community_id, request_id, user_id, tier_id, amount_cents,
       extend_days, membership_id, recorded_by, created_at
FROM payments WHERE community_id = $1 AND request_id = $2;

-- name: CreatePayment :one
INSERT INTO payments (community_id, request_id, user_id, tier_id, amount_cents,
                      extend_days, membership_id, recorded_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id, community_id, request_id, user_id, tier_id, amount_cents,
          extend_days, membership_id, recorded_by, created_at;
