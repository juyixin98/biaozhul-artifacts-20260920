package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/money"
)

// CreateMerchantInput creates a merchant plus its five accounts and an
// initial admin-issued operator key.
type CreateMerchantInput struct {
	Name          string `json:"name"`
	FeeBPS        int    `json:"fee_bps,omitempty"`
	FeeFixedCents int64  `json:"fee_fixed_cents,omitempty"`
}

type MerchantWithKeys struct {
	Merchant    db.Merchant `json:"merchant"`
	OperatorKey string      `json:"operator_key,omitempty"` // plaintext, only at creation
}

func (s *Service) CreateMerchant(ctx context.Context, actor Actor, in CreateMerchantInput) (MerchantWithKeys, error) {
	if actor.Role != "admin" {
		return MerchantWithKeys{}, domain.ErrForbidden
	}
	if in.Name == "" {
		return MerchantWithKeys{}, fmt.Errorf("%w: name required", domain.ErrValidation)
	}
	if in.FeeBPS == 0 {
		in.FeeBPS = money.DefaultFeeBPS
	}
	if in.FeeFixedCents == 0 {
		in.FeeFixedCents = money.DefaultFeeFixedCents
	}

	var out MerchantWithKeys
	err := s.retryTx(ctx, func(q *db.Queries) error {
		if err := ensureInternalAccounts(ctx, q); err != nil {
			return err
		}
		m, err := q.CreateMerchant(ctx, db.CreateMerchantParams{
			Name:          in.Name,
			FeeBps:        int32(in.FeeBPS),
			FeeFixedCents: in.FeeFixedCents,
		})
		if err != nil {
			return err
		}
		// Each merchant has its own ledger accounts so balances and
		// reconciliation are per-merchant, plus a suspense account for
		// corrective reversing entries.
		for _, a := range []struct{ code, kind, desc string }{
			{AcctGatewayCash, "asset", "Merchant simulated gateway cash"},
			{AcctFeeRevenue, "equity", "Merchant withheld fees"},
			{AcctPayable, "liability", "Net funds owed/paid to merchant"},
			{AcctSuspense, "equity", "Merchant correction suspense account"},
		} {
			mid := m.ID
			if _, err := q.CreateAccount(ctx, db.CreateAccountParams{
				MerchantID:  &mid,
				Code:        a.code,
				Kind:        a.kind,
				Description: a.desc,
			}); err != nil {
				return err
			}
		}
		// Issue one operator key immediately so the merchant can transact.
		plain, hash, prefix, err := GenerateAPIKey("sk_op_")
		if err != nil {
			return err
		}
		if _, err := q.CreateAPIKey(ctx, db.CreateAPIKeyParams{
			KeyHash:    hash,
			KeyPrefix:  prefix,
			MerchantID: &m.ID,
			Role:       "operator",
			Label:      "initial operator key",
		}); err != nil {
			return err
		}
		if err := audit(ctx, q, actor, &m.ID, "merchant.create", "merchant",
			m.ID.String(), []byte(fmt.Sprintf(`{"name":%q}`, in.Name))); err != nil {
			return err
		}
		out = MerchantWithKeys{Merchant: m, OperatorKey: plain}
		return nil
	})
	return out, err
}

func (s *Service) GetMerchant(ctx context.Context, actor Actor, id uuid.UUID) (db.Merchant, error) {
	if err := actor.requireMerchant(id); err != nil {
		return db.Merchant{}, err
	}
	m, err := s.q.GetMerchant(ctx, id)
	if errors.Is(err, pgxNoRows) {
		return db.Merchant{}, domain.ErrNotFound
	}
	return m, err
}

func (s *Service) ListMerchants(ctx context.Context, actor Actor) ([]db.Merchant, error) {
	if actor.Role == "admin" {
		return s.q.ListMerchants(ctx)
	}
	// Operators/auditors only see their own merchant.
	if actor.MerchantID == nil {
		return nil, domain.ErrForbidden
	}
	m, err := s.q.GetMerchant(ctx, *actor.MerchantID)
	if err != nil {
		return nil, err
	}
	return []db.Merchant{m}, nil
}

type UpdateMerchantInput struct {
	Name          *string `json:"name,omitempty"`
	FeeBPS        *int    `json:"fee_bps,omitempty"`
	FeeFixedCents *int64  `json:"fee_fixed_cents,omitempty"`
	Active        *bool   `json:"active,omitempty"`
}

func (s *Service) UpdateMerchant(ctx context.Context, actor Actor, id uuid.UUID, in UpdateMerchantInput) (db.Merchant, error) {
	if actor.Role != "admin" {
		return db.Merchant{}, domain.ErrForbidden
	}
	var out db.Merchant
	err := s.retryTx(ctx, func(q *db.Queries) error {
		m, err := q.GetMerchant(ctx, id)
		if errors.Is(err, pgxNoRows) {
			return domain.ErrNotFound
		}
		if err != nil {
			return err
		}
		if in.Name != nil {
			m.Name = *in.Name
		}
		if in.FeeBPS != nil {
			m.FeeBps = int32(*in.FeeBPS)
		}
		if in.FeeFixedCents != nil {
			m.FeeFixedCents = *in.FeeFixedCents
		}
		if in.Active != nil {
			m.Active = *in.Active
		}
		out, err = q.UpdateMerchant(ctx, db.UpdateMerchantParams{
			ID:            m.ID,
			Name:          m.Name,
			FeeBps:        m.FeeBps,
			FeeFixedCents: m.FeeFixedCents,
			Active:        m.Active,
		})
		if err != nil {
			return err
		}
		return audit(ctx, q, actor, &id, "merchant.update", "merchant", id.String(), []byte("{}"))
	})
	return out, err
}

// IssueAPIKey lets an admin mint keys for any role. Operator/auditor keys must
// carry a merchant id; admin keys must not.
type IssueKeyInput struct {
	Role       string     `json:"role"`
	MerchantID *uuid.UUID `json:"merchant_id,omitempty"`
	Label      string     `json:"label,omitempty"`
}

type IssuedKey struct {
	Key    string    `json:"api_key"`
	KeyID  uuid.UUID `json:"key_id"`
	Prefix string    `json:"prefix"`
	Role   string    `json:"role"`
}

func (s *Service) IssueAPIKey(ctx context.Context, actor Actor, in IssueKeyInput) (IssuedKey, error) {
	if actor.Role != "admin" {
		return IssuedKey{}, domain.ErrForbidden
	}
	switch in.Role {
	case "operator", "auditor":
		if in.MerchantID == nil {
			return IssuedKey{}, fmt.Errorf("%w: %s keys require merchant_id", domain.ErrValidation, in.Role)
		}
	case "admin":
		in.MerchantID = nil
	default:
		return IssuedKey{}, fmt.Errorf("%w: role must be admin, operator or auditor", domain.ErrValidation)
	}
	plainPrefix := map[string]string{
		"admin": "sk_ad_", "operator": "sk_op_", "auditor": "sk_au_",
	}[in.Role]
	plain, hash, prefix, err := GenerateAPIKey(plainPrefix)
	if err != nil {
		return IssuedKey{}, err
	}
	var rec db.ApiKey
	err = s.inTx(ctx, func(q *db.Queries) error {
		if in.MerchantID != nil {
			if _, err := q.GetMerchant(ctx, *in.MerchantID); err != nil {
				if errors.Is(err, pgx.ErrNoRows) {
					return domain.ErrNotFound
				}
				return err
			}
		}
		rec, err = q.CreateAPIKey(ctx, db.CreateAPIKeyParams{
			KeyHash:    hash,
			KeyPrefix:  prefix,
			MerchantID: in.MerchantID,
			Role:       in.Role,
			Label:      in.Label,
		})
		if err != nil {
			return err
		}
		return audit(ctx, q, actor, rec.MerchantID, "api_key.issue", "api_key",
			rec.ID.String(), []byte(fmt.Sprintf(`{"role":%q}`, in.Role)))
	})
	if err != nil {
		return IssuedKey{}, err
	}
	return IssuedKey{Key: plain, KeyID: rec.ID, Prefix: rec.KeyPrefix, Role: rec.Role}, nil
}

// GenerateAPIKey returns a new random secret plus its SHA-256 hash and a
// display prefix. The plaintext key is shown once; only the hash is stored.
func GenerateAPIKey(prefix string) (plain, hash, displayPrefix string, err error) {
	b := make([]byte, 24)
	if _, err = rand.Read(b); err != nil {
		return
	}
	plain = prefix + hex.EncodeToString(b)
	sum := sha256.Sum256([]byte(plain))
	hash = hex.EncodeToString(sum[:])
	displayPrefix = plain[:12]
	return
}

func HashAPIKey(plain string) string {
	sum := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(sum[:])
}
