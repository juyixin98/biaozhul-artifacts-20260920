package service

import (
	"context"

	"github.com/jmoiron/sqlx"
)

// Visibility rules:
//   - customers see only their own domains/transfers;
//   - resellers see only domains of their own customers, plus their own
//     balance and ledger;
//   - admins see everything and manage prices/credits.

func (s *Service) ListDomains(ctx context.Context, user *User) ([]Domain, error) {
	var out []Domain
	var err error
	base := `SELECT id, name, owner_id, status, expires_at, version, created_at, updated_at FROM domains`
	switch user.Role {
	case RoleAdmin:
		err = s.db.SelectContext(ctx, &out, base+` ORDER BY name`)
	case RoleReseller:
		err = s.db.SelectContext(ctx, &out, base+`
			WHERE owner_id IN (SELECT id FROM users WHERE reseller_id = $1) ORDER BY name`, user.ID)
	default:
		err = s.db.SelectContext(ctx, &out, base+` WHERE owner_id = $1 ORDER BY name`, user.ID)
	}
	return out, err
}

func (s *Service) GetDomain(ctx context.Context, user *User, rawName string) (*Domain, error) {
	name, err := normalizeName(rawName)
	if err != nil {
		return nil, err
	}
	var dom Domain
	err = s.db.GetContext(ctx, &dom,
		`SELECT id, name, owner_id, status, expires_at, version, created_at, updated_at FROM domains WHERE name = $1`, name)
	if err != nil {
		return nil, ErrNotFound
	}
	switch user.Role {
	case RoleAdmin:
		return &dom, nil
	case RoleReseller:
		var n int
		if err := s.db.GetContext(ctx, &n,
			`SELECT COUNT(1) FROM users WHERE id = $1 AND reseller_id = $2`, dom.OwnerID, user.ID); err != nil {
			return nil, err
		}
		if n == 0 {
			return nil, ErrForbidden
		}
		return &dom, nil
	default:
		if dom.OwnerID != user.ID {
			return nil, ErrForbidden
		}
		return &dom, nil
	}
}

func (s *Service) ListTransfers(ctx context.Context, user *User) ([]Transfer, error) {
	var out []Transfer
	var err error
	base := `
		SELECT t.id, t.domain_id, d.name AS domain_name, t.from_owner_id, t.to_owner_id, t.state,
		       t.price_cents, t.hold_id, t.approval_deadline, t.completes_at, t.cancel_reason, t.created_at
		FROM transfers t JOIN domains d ON d.id = t.domain_id`
	switch user.Role {
	case RoleAdmin:
		err = s.db.SelectContext(ctx, &out, base+` ORDER BY t.created_at DESC`)
	case RoleReseller:
		err = s.db.SelectContext(ctx, &out, base+`
			WHERE t.from_owner_id IN (SELECT id FROM users WHERE reseller_id = $1)
			   OR t.to_owner_id IN (SELECT id FROM users WHERE reseller_id = $1)
			ORDER BY t.created_at DESC`, user.ID)
	default:
		err = s.db.SelectContext(ctx, &out, base+`
			WHERE t.from_owner_id = $1 OR t.to_owner_id = $1 ORDER BY t.created_at DESC`, user.ID)
	}
	return out, err
}

func (s *Service) ResellerBalance(ctx context.Context, resellerID string) (*Balance, error) {
	b, err := balance(ctx, s.db, resellerID)
	if err != nil {
		return nil, err
	}
	h, err := heldAmount(ctx, s.db, resellerID)
	if err != nil {
		return nil, err
	}
	return &Balance{
		ResellerID:     resellerID,
		BalanceCents:   b,
		HeldCents:      h,
		AvailableCents: b - h,
	}, nil
}

func (s *Service) ResellerLedger(ctx context.Context, resellerID string) ([]LedgerEntry, error) {
	var out []LedgerEntry
	err := s.db.SelectContext(ctx, &out,
		`SELECT id, reseller_id, idempotency_key, kind, amount_cents, memo, domain_name, transfer_id, created_at
		 FROM ledger_entries WHERE reseller_id = $1 ORDER BY id`, resellerID)
	return out, err
}

// CreditReseller tops up a reseller's credit balance (admin). Idempotent.
func (s *Service) CreditReseller(ctx context.Context, admin *User, resellerID string, amount int64, idemKey string) (int, any, error) {
	if admin.Role != RoleAdmin {
		return 0, nil, ErrForbidden
	}
	var role string
	if err := s.db.GetContext(ctx, &role, `SELECT role FROM users WHERE id = $1`, resellerID); err != nil {
		return 0, nil, ErrNotFound
	}
	if role != RoleReseller {
		return 0, nil, ErrValidation("target user is not a reseller")
	}
	return s.idempotent(ctx, idemKey, admin.ID, "credit", func(ctx context.Context, tx *sqlx.Tx) (int, any, error) {
		if err := credit(ctx, tx, resellerID, amount, admin.ID+":"+idemKey+":credit", "admin top-up"); err != nil {
			return 0, nil, err
		}
		bal, err := balance(ctx, tx, resellerID)
		if err != nil {
			return 0, nil, err
		}
		return 200, map[string]any{"reseller_id": resellerID, "balance_cents": bal}, nil
	})
}
