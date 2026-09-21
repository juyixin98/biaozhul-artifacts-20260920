package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
)

// CorrectionInput posts an admin correction. Historical ledger entries are
// never modified; a correction is a new balanced pair that reverses the
// effect of a wrong posting through the merchant suspense account.
//
// Example: gateway_cash was overstated by 100 cents:
//
//	gateway_cash -100, suspense +100
//
// The sign is taken from the internal account leg.
type CorrectionInput struct {
	MerchantID  uuid.UUID `json:"merchant_id"`
	AccountCode string    `json:"account_code"` // one of the internal account codes
	AmountCents int64     `json:"amount_cents"` // signed: negative reduces that account
	Reason      string    `json:"reason"`
}

func (s *Service) PostCorrection(ctx context.Context, actor Actor, in CorrectionInput) (uuid.UUID, error) {
	if actor.Role != "admin" {
		return uuid.Nil, domain.ErrForbidden
	}
	if in.AmountCents == 0 {
		return uuid.Nil, fmt.Errorf("%w: correction amount must be non-zero", domain.ErrValidation)
	}
	switch in.AccountCode {
	case AcctGatewayCash, AcctFeeRevenue, AcctPayable:
	default:
		return uuid.Nil, fmt.Errorf("%w: unsupported account %q", domain.ErrValidation, in.AccountCode)
	}
	if in.Reason == "" {
		return uuid.Nil, fmt.Errorf("%w: reason required for corrections", domain.ErrValidation)
	}

	refID := uuid.New()
	err := s.inTx(ctx, func(q *db.Queries) error {
		legs := []Posting{
			{merchantAccount(in.MerchantID, in.AccountCode), in.AmountCents},
			{merchantAccount(in.MerchantID, AcctSuspense), -in.AmountCents},
		}
		if err := postEntries(ctx, q, s.clock.Now(), "correction", "manual", refID, legs); err != nil {
			return err
		}
		return audit(ctx, q, actor, &in.MerchantID, "ledger.correction", "manual",
			refID.String(), []byte(fmt.Sprintf(`{"account":%q,"amount":%d,"reason":%q}`,
				in.AccountCode, in.AmountCents, in.Reason)))
	})
	if err != nil {
		return uuid.Nil, err
	}
	return refID, nil
}

// LedgerAccount is a masked, read-only account view for auditors/operators.
type LedgerAccount struct {
	Code       string     `json:"code"`
	MerchantID *uuid.UUID `json:"merchant_id"`
	Balance    int64      `json:"balance_cents"`
}

func (s *Service) AccountBalances(ctx context.Context, actor Actor, merchantID uuid.UUID) ([]LedgerAccount, error) {
	if err := actor.requireMerchant(merchantID); err != nil {
		return nil, err
	}
	codes := []string{AcctGatewayCash, AcctFeeRevenue, AcctPayable, AcctSuspense}
	out := make([]LedgerAccount, 0, len(codes))
	for _, c := range codes {
		bal, err := accountBalance(ctx, s.q, merchantAccount(merchantID, c))
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				continue
			}
			return nil, err
		}
		mid := merchantID
		out = append(out, LedgerAccount{Code: c, MerchantID: &mid, Balance: bal})
	}
	return out, nil
}

// LedgerEntryView is a read view with the account code resolved.
type LedgerEntryView struct {
	ID          int64     `json:"id"`
	TxType      string    `json:"tx_type"`
	RefType     string    `json:"ref_type"`
	RefID       uuid.UUID `json:"ref_id"`
	AccountCode string    `json:"account_code"`
	AmountCents int64     `json:"amount_cents"`
	CreatedAt   string    `json:"created_at"`
}

// ListLedgerEntries returns ledger entries for a reference: a payment id, a
// refund id or a batch id. Access is scoped to the caller's merchant (admins
// see everything). Ledger rows are immutable and never expose card data.
func (s *Service) ListLedgerEntries(ctx context.Context, actor Actor,
	refType string, refID uuid.UUID) ([]LedgerEntryView, error) {
	switch refType {
	case "payment", "refund", "batch", "manual":
	default:
		return nil, fmt.Errorf("%w: ref_type must be payment, refund, batch or manual",
			domain.ErrValidation)
	}

	// Resolve which merchant the reference belongs to and enforce scoping.
	switch refType {
	case "payment":
		p, err := s.q.GetPayment(ctx, refID)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, domain.ErrNotFound
		}
		if err != nil {
			return nil, err
		}
		if err := actor.requireMerchant(p.MerchantID); err != nil {
			return nil, err
		}
	case "refund":
		r, err := s.q.GetRefund(ctx, refID)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, domain.ErrNotFound
		}
		if err != nil {
			return nil, err
		}
		if err := actor.requireMerchant(r.MerchantID); err != nil {
			return nil, err
		}
	case "batch":
		b, err := s.q.GetBatch(ctx, refID)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, domain.ErrNotFound
		}
		if err != nil {
			return nil, err
		}
		if err := actor.requireMerchant(b.MerchantID); err != nil {
			return nil, err
		}
	}

	entries, err := s.q.ListEntriesByRef(ctx, db.ListEntriesByRefParams{
		RefType: refType, RefID: refID,
	})
	if err != nil {
		return nil, err
	}
	out := make([]LedgerEntryView, 0, len(entries))
	for _, e := range entries {
		acct, err := s.q.GetAccountByID(ctx, e.AccountID)
		if err != nil {
			return nil, err
		}
		// Non-admins cannot see another merchant's suspense leg.
		if acct.MerchantID != nil {
			if actor.Role != "admin" &&
				(actor.MerchantID == nil || *actor.MerchantID != *acct.MerchantID) {
				continue
			}
		}
		out = append(out, LedgerEntryView{
			ID:          e.ID,
			TxType:      e.TxType,
			RefType:     e.RefType,
			RefID:       e.RefID,
			AccountCode: acct.Code,
			AmountCents: e.AmountCents,
			CreatedAt:   e.CreatedAt.Time.Format(time.RFC3339),
		})
	}
	return out, nil
}
