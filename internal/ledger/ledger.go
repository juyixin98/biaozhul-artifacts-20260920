// Package ledger wraps posting rules around the append-only ledger_entries table.
// Every Posting is a balanced set of signed lines sharing one (refType, refID).
// Historical entries are immutable at the database level (RULEs reject UPDATE
// and DELETE); corrections are separate, explicit reversing postings.
package ledger

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/clearsettle/clearsettle/internal/store"
)

const (
	RefTypePayment    = "payment"
	RefTypeRefund     = "refund"
	RefTypeSettlement = "settlement"
	RefTypeCorrection = "correction"

	CodeCash       = "CASH"
	CodeFeeRevenue = "FEE_REVENUE"
)

func PayableCode(merchantID uuid.UUID) string { return "PAYABLE:" + merchantID.String() }

type Line struct {
	AccountID uuid.UUID
	Amount    int64 // signed cents
}

type Posting struct {
	RefType string
	RefID   uuid.UUID
	Lines   []Line
}

func (p Posting) validate() error {
	if len(p.Lines) < 2 {
		return fmt.Errorf("posting %s/%s needs >= 2 lines", p.RefType, p.RefID)
	}
	var sum int64
	seen := map[uuid.UUID]bool{}
	for _, l := range p.Lines {
		if l.Amount == 0 {
			return fmt.Errorf("zero-amount ledger line for account %s", l.AccountID)
		}
		if seen[l.AccountID] {
			return fmt.Errorf("duplicate account %s in posting", l.AccountID)
		}
		seen[l.AccountID] = true
		sum += l.Amount
	}
	if sum != 0 {
		return fmt.Errorf("unbalanced posting %s/%s: sum=%d", p.RefType, p.RefID, sum)
	}
	return nil
}

// EnsureMerchantAccount creates the merchant payable liability account once.
func EnsureMerchantAccount(ctx context.Context, q store.Querier, merchantID uuid.UUID) error {
	code := PayableCode(merchantID)
	if _, err := q.GetLedgerAccountByCode(ctx, code); err == nil {
		return nil
	} else if err != pgx.ErrNoRows {
		return err
	}
	_, err := q.CreateLedgerAccount(ctx, store.CreateLedgerAccountParams{
		Code: code,
		Name: "Merchant payable " + merchantID.String(),
		Kind: "liability",
	})
	if err != nil && !isUniqueViolation(err) {
		return err
	}
	return nil
}

// Post writes a balanced posting inside the caller's transaction. Account rows
// are locked first so concurrent postings serialize but don't deadlock against
// balance reads. The deferred DB trigger re-checks balance at commit.
func Post(ctx context.Context, tx pgx.Tx, q store.Querier, p Posting) error {
	if err := p.validate(); err != nil {
		return err
	}
	// Lock accounts in a deterministic order (uuid lexical) to avoid deadlocks
	// between concurrent postings that touch the same accounts.
	ids := make([]uuid.UUID, 0, len(p.Lines))
	for _, l := range p.Lines {
		ids = append(ids, l.AccountID)
	}
	for i := 1; i < len(ids); i++ {
		for j := i; j > 0 && ids[j].String() < ids[j-1].String(); j-- {
			ids[j], ids[j-1] = ids[j-1], ids[j]
		}
	}
	for _, id := range ids {
		if _, err := tx.Exec(ctx,
			`SELECT id FROM ledger_accounts WHERE id=$1 FOR UPDATE`, id); err != nil {
			return err
		}
	}
	for _, l := range p.Lines {
		if err := q.InsertLedgerEntry(ctx, store.InsertLedgerEntryParams{
			AccountID: l.AccountID,
			RefType:   p.RefType,
			RefID:     p.RefID,
			Amount:    l.Amount,
		}); err != nil {
			return err
		}
	}
	return nil
}

// Accounts resolves the three accounts used in business postings.
type Accounts struct {
	Cash    store.LedgerAccount
	Fees    store.LedgerAccount
	Payable store.LedgerAccount
}

func LoadAccounts(ctx context.Context, q store.Querier, merchantID uuid.UUID) (Accounts, error) {
	var a Accounts
	var err error
	a.Cash, err = q.GlobalAccountForUpdate(ctx, CodeCash)
	if err != nil {
		return a, err
	}
	a.Fees, err = q.GlobalAccountForUpdate(ctx, CodeFeeRevenue)
	if err != nil {
		return a, err
	}
	a.Payable, err = q.PayableAccountForUpdate(ctx, merchantID.String())
	return a, err
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}
