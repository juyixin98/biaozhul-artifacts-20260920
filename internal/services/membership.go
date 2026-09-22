package services

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"

	"communitygov/internal/database"
	"communitygov/internal/database/sqlcgen"
)

type MembershipService struct{ store *database.Store }

func NewMembershipService(s *database.Store) *MembershipService {
	return &MembershipService{store: s}
}

func (s *MembershipService) CreateTier(ctx context.Context, communityID int64, level int32, name string,
	priceCents int64, durationDays int32) (sqlcgen.Tier, error) {
	if level < 1 || level > 10 {
		return sqlcgen.Tier{}, E(ErrMaxTiers, "level must be 1..10")
	}
	if priceCents < 0 || durationDays <= 0 {
		return sqlcgen.Tier{}, E(ErrValidation, "price must be >= 0 cents and duration positive")
	}
	var tier sqlcgen.Tier
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		if _, err := q.GetCommunity(ctx, communityID); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return E(ErrNotFound, "community")
			}
			return err
		}
		cnt, err := q.CountTiers(ctx, communityID)
		if err != nil {
			return err
		}
		if cnt >= 10 {
			return E(ErrTierLimit, "")
		}
		tier, err = q.CreateTier(ctx, sqlcgen.CreateTierParams{
			CommunityID: communityID, Level: level, Name: name,
			PriceCents: priceCents, DurationDays: durationDays,
		})
		if isCheckViolation(err) || isUniqueViolation(err) {
			return E(ErrTierLimit, "tier level already used or 10-tier limit reached")
		}
		return err
	})
	return tier, err
}

func (s *MembershipService) ListTiers(ctx context.Context, communityID int64) ([]sqlcgen.Tier, error) {
	return s.store.ListTiers(ctx, communityID)
}

// RecordPaymentParams is the full idempotent payment payload.
type RecordPaymentParams struct {
	CommunityID int64
	RequestID   string
	UserID      int64
	TierID      int64
	AmountCents int64
	ExtendDays  int32
	RecordedBy  int64
}

// RecordPayment registers an offline (manually confirmed) payment and extends
// the target user's membership.
//
// Idempotency: (community_id, request_id) is unique. Replaying the same
// request id with an identical payload returns the original payment without
// touching expiry; a replay with a different payload is 409 Conflict.
//
// No lost renewals: membership is locked SELECT ... FOR UPDATE for the whole
// transaction and extension computes from GREATEST(expires_at, now()), so two
// concurrent renewals serialize and both add their full durations.
func (s *MembershipService) RecordPayment(ctx context.Context, in RecordPaymentParams) (sqlcgen.Payment, sqlcgen.Membership, error) {
	var payment sqlcgen.Payment
	var membership sqlcgen.Membership
	err := s.store.InTx(ctx, func(ctx context.Context, q *sqlcgen.Queries) error {
		// 1. Idempotency probe.
		existing, err := q.FindPaymentByRequestID(ctx,
			sqlcgen.FindPaymentByRequestIDParams{CommunityID: in.CommunityID, RequestID: in.RequestID})
		if err == nil {
			if existing.UserID != in.UserID || existing.TierID != in.TierID ||
				existing.AmountCents != in.AmountCents || existing.ExtendDays != in.ExtendDays {
				return E(ErrConflict, "request id already used with a different payload")
			}
			payment = existing
			membership, err = q.GetMembership(ctx,
				sqlcgen.GetMembershipParams{CommunityID: in.CommunityID, UserID: in.UserID})
			return err
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}

		// 2. Validate tier belongs to this community.
		tier, err := q.GetTier(ctx, in.TierID)
		if errors.Is(err, pgx.ErrNoRows) {
			return E(ErrNotFound, "tier")
		} else if err != nil {
			return err
		}
		if tier.CommunityID != in.CommunityID {
			return E(ErrValidation, "tier belongs to another community")
		}
		if in.AmountCents < 0 {
			return E(ErrValidation, "amount_cents must be >= 0")
		}
		if in.ExtendDays <= 0 {
			return E(ErrValidation, "extend_days must be positive")
		}

		// 3. Lock membership (or create). FOR UPDATE serializes renewals.
		m, err := q.GetMembershipForUpdate(ctx,
			sqlcgen.GetMembershipForUpdateParams{CommunityID: in.CommunityID, UserID: in.UserID})
		if errors.Is(err, pgx.ErrNoRows) {
			m, err = q.CreateMembershipIfMissing(ctx, sqlcgen.CreateMembershipIfMissingParams{
				CommunityID: in.CommunityID,
				UserID:      in.UserID,
				TierID:      in.TierID,
				Column4:     in.ExtendDays,
			})
			if errors.Is(err, pgx.ErrNoRows) {
				// A concurrent first renewal inserted it first: re-lock that row.
				m, err = q.GetMembershipForUpdate(ctx,
					sqlcgen.GetMembershipForUpdateParams{CommunityID: in.CommunityID, UserID: in.UserID})
			}
			if err != nil {
				return err
			}
		} else if err != nil {
			return err
		}

		// 4. Extend from the later of current expiry and now (cancelled or
		// expired memberships restart from now; active ones keep all time).
		m, err = q.ExtendMembership(ctx, sqlcgen.ExtendMembershipParams{
			ID: m.ID, CommunityID: in.CommunityID, TierID: in.TierID, Column4: in.ExtendDays,
		})
		if err != nil {
			return err
		}

		// 5. Record payment; unique constraint is the hard idempotency guard.
		payment, err = q.CreatePayment(ctx, sqlcgen.CreatePaymentParams{
			CommunityID: in.CommunityID, RequestID: in.RequestID,
			UserID: in.UserID, TierID: in.TierID, AmountCents: in.AmountCents,
			ExtendDays: in.ExtendDays, MembershipID: m.ID, RecordedBy: in.RecordedBy,
		})
		if err != nil {
			return err
		}
		membership = m
		return nil
	})
	return payment, membership, err
}

// Cancel immediately revokes access: membership.status='cancelled' and every
// access check requires status='active' AND expires_at > now().
func (s *MembershipService) Cancel(ctx context.Context, communityID, userID int64) (sqlcgen.Membership, error) {
	m, err := s.store.CancelMembership(ctx,
		sqlcgen.CancelMembershipParams{CommunityID: communityID, UserID: userID})
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.Membership{}, E(ErrNotFound, "membership")
	}
	return m, err
}

func (s *MembershipService) Get(ctx context.Context, communityID, userID int64) (sqlcgen.Membership, error) {
	m, err := s.store.GetMembership(ctx,
		sqlcgen.GetMembershipParams{CommunityID: communityID, UserID: userID})
	if errors.Is(err, pgx.ErrNoRows) {
		return sqlcgen.Membership{}, E(ErrNotFound, "membership")
	}
	return m, err
}
