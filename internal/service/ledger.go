package service

import (
	"context"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/clearsettle/clearsettle/internal/db"
)

// Account codes. gateway_cash, fee_revenue, unsettled and payable are single
// platform-level accounts; every merchant also has a suspense account used for
// corrective entries.
const (
	AcctGatewayCash = "gateway_cash" // asset: simulated cash held by the platform
	AcctFeeRevenue  = "fee_revenue"  // revenue: withheld processing fees
	AcctUnsettled   = "unsettled"    // liability: captured funds not yet settled
	AcctPayable     = "payable"      // liability: net funds owed / paid to merchant
	AcctSuspense    = "suspense"     // equity-like: correction buffer per merchant
)

// Posting is one leg of a double-entry move. The ledger enforces balanced
// postings (sum of amounts == 0) at the service layer.
type Posting struct {
	AccountKey accountKey
	Amount     int64
}

type accountKey struct {
	MerchantID *uuid.UUID // nil for internal accounts
	Code       string
}

func internalAccount(code string) accountKey { return accountKey{Code: code} }
func merchantAccount(merchantID uuid.UUID, code string) accountKey {
	return accountKey{MerchantID: &merchantID, Code: code}
}

// postEntries writes balanced ledger legs for a referenced business event.
// Accounts must already exist (they are created with the merchant).
func postEntries(
	ctx context.Context,
	q *db.Queries,
	at time.Time,
	txType, refType string,
	refID uuid.UUID,
	legs []Posting,
) error {
	var sum int64
	for _, leg := range legs {
		sum += leg.Amount
	}
	if sum != 0 {
		return ErrUnbalancedEntry
	}
	for _, leg := range legs {
		acct, err := accountByKey(ctx, q, leg.AccountKey)
		if err != nil {
			return err
		}
		if err := q.InsertLedgerEntry(ctx, db.InsertLedgerEntryParams{
			TxType:      txType,
			RefType:     refType,
			RefID:       refID,
			AccountID:   acct.ID,
			AmountCents: leg.Amount,
			CreatedAt:   pgtype.Timestamptz{Time: at, Valid: true},
		}); err != nil {
			return err
		}
	}
	return nil
}

func accountByKey(ctx context.Context, q *db.Queries, key accountKey) (db.Account, error) {
	return q.GetAccount(ctx, db.GetAccountParams{MerchantID: key.MerchantID, Code: key.Code})
}

// accountBalance returns the signed sum of entries for an account.
func accountBalance(ctx context.Context, q *db.Queries, key accountKey) (int64, error) {
	acct, err := accountByKey(ctx, q, key)
	if err != nil {
		return 0, err
	}
	return q.SumAccountEntries(ctx, acct.ID)
}

// ensureInternalAccounts creates the four platform accounts once.
func ensureInternalAccounts(ctx context.Context, q *db.Queries) error {
	internals := []struct {
		code, kind, desc string
	}{
		{AcctGatewayCash, "asset", "Simulated gateway cash account"},
		{AcctFeeRevenue, "equity", "Withheld processing fees (equity contra)"},
		{AcctUnsettled, "liability", "Reserved held-authorization account"},
		{AcctPayable, "liability", "Net funds owed/paid to merchants"},
	}
	for _, a := range internals {
		if _, err := q.GetAccount(ctx, db.GetAccountParams{MerchantID: nil, Code: a.code}); err != nil {
			if err == pgx.ErrNoRows {
				if _, err := q.CreateAccount(ctx, db.CreateAccountParams{
					MerchantID:  nil,
					Code:        a.code,
					Kind:        a.kind,
					Description: a.desc,
				}); err != nil {
					return err
				}
				continue
			}
			return err
		}
	}
	return nil
}
