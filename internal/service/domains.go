package service

import (
	"context"
	"errors"

	"github.com/google/uuid"
	"github.com/jmoiron/sqlx"
	"github.com/lib/pq"

	"domainengine/internal/domainname"
)

func isUniqueViolation(err error) bool {
	var pqErr *pq.Error
	return errors.As(err, &pqErr) && pqErr.Code == "23505"
}

func resellerOf(user *User) (string, error) {
	if user.Role != RoleCustomer || user.ResellerID == nil {
		return "", ErrForbidden
	}
	return *user.ResellerID, nil
}

// Register claims an available name. Concurrency safety comes from the
// UNIQUE(name) constraint: exactly one concurrent insert wins, the rest get
// 409, and only the winner's charge commits.
func (s *Service) Register(ctx context.Context, user *User, rawName string, years int, idemKey string) (int, any, error) {
	resellerID, err := resellerOf(user)
	if err != nil {
		return 0, nil, err
	}
	name, err := domainname.Normalize(rawName)
	if err != nil {
		return 0, nil, ErrValidation("invalid domain name: %v", err)
	}
	if years < 1 || years > 10 {
		return 0, nil, ErrValidation("years must be between 1 and 10")
	}
	now := s.clock.Now()
	price, err := s.priceAt(ctx, domainname.TLD(name), ActionRegister, now)
	if err != nil {
		return 0, nil, err
	}
	total := price * int64(years)

	return s.idempotent(ctx, idemKey, user.ID, "register", func(ctx context.Context, tx *sqlx.Tx) (int, any, error) {
		if err := ensureFunds(ctx, tx, resellerID, total); err != nil {
			return 0, nil, err
		}
		dom := Domain{
			ID:        uuid.NewString(),
			Name:      name,
			OwnerID:   user.ID,
			Status:    StatusRegistered,
			ExpiresAt: now.AddDate(years, 0, 0),
			CreatedAt: now,
			UpdatedAt: now,
		}
		_, err := tx.ExecContext(ctx,
			`INSERT INTO domains (id, name, owner_id, status, expires_at) VALUES ($1, $2, $3, $4, $5)`,
			dom.ID, dom.Name, dom.OwnerID, dom.Status, dom.ExpiresAt)
		if isUniqueViolation(err) {
			return 0, nil, ErrDomainTaken
		}
		if err != nil {
			return 0, nil, err
		}
		if err := charge(ctx, tx, resellerID, total, idemKey+":register:"+name, "register "+name, name, nil); err != nil {
			return 0, nil, err
		}
		if err := s.addEvent(ctx, tx, &dom.ID, name, "registered", map[string]any{
			"years": years, "price_cents": total, "owner_id": user.ID,
		}); err != nil {
			return 0, nil, err
		}
		return 201, dom, nil
	})
}

// Renew extends a registration. It is also the redemption path: in
// `redemption` status the redeem price applies. The domain row is locked
// FOR UPDATE, so a renewal racing the expiry job is serialized: either the
// renewal commits first (job then sees expires_at > now and skips the row)
// or the job commits first (renewal then proceeds from `expired` status).
// Both orders end in `registered` with exactly one charge.
func (s *Service) Renew(ctx context.Context, user *User, rawName string, years int, idemKey string) (int, any, error) {
	name, err := domainname.Normalize(rawName)
	if err != nil {
		return 0, nil, ErrValidation("invalid domain name: %v", err)
	}
	if years < 1 || years > 10 {
		return 0, nil, ErrValidation("years must be between 1 and 10")
	}
	now := s.clock.Now()

	return s.idempotent(ctx, idemKey, user.ID, "renew", func(ctx context.Context, tx *sqlx.Tx) (int, any, error) {
		dom, err := lockDomain(ctx, tx, name)
		if err != nil {
			return 0, nil, err
		}
		if dom.OwnerID != user.ID && user.Role != RoleAdmin {
			return 0, nil, ErrForbidden
		}
		action := ActionRenew
		switch dom.Status {
		case StatusRegistered, StatusExpired:
			action = ActionRenew
		case StatusRedemption:
			action = ActionRedeem
		default:
			return 0, nil, ErrInvalidState("domain in status %q cannot be renewed", dom.Status)
		}
		price, err := s.priceAt(ctx, domainname.TLD(name), action, now)
		if err != nil {
			return 0, nil, err
		}
		total := price * int64(years)

		owner, err := getUser(ctx, tx, dom.OwnerID)
		if err != nil {
			return 0, nil, err
		}
		resellerID, err := resellerOf(owner)
		if err != nil {
			return 0, nil, err
		}
		if err := ensureFunds(ctx, tx, resellerID, total); err != nil {
			return 0, nil, err
		}

		base := dom.ExpiresAt
		if base.Before(now) {
			base = now
		}
		dom.ExpiresAt = base.AddDate(years, 0, 0)
		dom.Status = StatusRegistered
		res, err := tx.ExecContext(ctx,
			`UPDATE domains SET status = $1, expires_at = $2, version = version + 1, updated_at = $3
			 WHERE id = $4 AND version = $5`,
			dom.Status, dom.ExpiresAt, now, dom.ID, dom.Version)
		if err != nil {
			return 0, nil, err
		}
		if n, _ := res.RowsAffected(); n == 0 {
			return 0, nil, ErrInvalidState("domain changed concurrently; retry")
		}
		if err := charge(ctx, tx, resellerID, total, idemKey+":"+action+":"+name, action+" "+name, name, nil); err != nil {
			return 0, nil, err
		}
		if err := s.addEvent(ctx, tx, &dom.ID, name, action+"ed", map[string]any{
			"years": years, "price_cents": total, "new_expires_at": dom.ExpiresAt,
		}); err != nil {
			return 0, nil, err
		}
		return 200, dom, nil
	})
}

func lockDomain(ctx context.Context, tx *sqlx.Tx, name string) (*Domain, error) {
	var dom Domain
	err := tx.GetContext(ctx, &dom,
		`SELECT id, name, owner_id, status, expires_at, version FROM domains WHERE name = $1 FOR UPDATE`, name)
	if err != nil {
		return nil, ErrNotFound
	}
	return &dom, nil
}

func getUser(ctx context.Context, tx *sqlx.Tx, id string) (*User, error) {
	var u User
	if err := tx.GetContext(ctx, &u, `SELECT id, api_key, role, name, reseller_id FROM users WHERE id = $1`, id); err != nil {
		return nil, err
	}
	return &u, nil
}
