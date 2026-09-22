package service

import (
	"context"
	"database/sql"
	"fmt"
)

// dbtx is satisfied by both *sqlx.DB and *sqlx.Tx, so ledger helpers can run
// standalone (read paths) or inside a caller's transaction (write paths).
type dbtx interface {
	GetContext(ctx context.Context, dest any, query string, args ...any) error
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}

// Ledger rules:
//   - ledger_entries is append-only; amounts are integer cents, always
//     positive, with `kind` giving the direction (credit in, charge out).
//   - balance(reseller) = sum(credit) - sum(charge)
//   - available(reseller) = balance - sum(open credit_holds)
//   - every ledger row carries a unique idempotency key; job-issued rows use
//     deterministic keys ("transfer:<id>:capture") so re-running a job after
//     a crash can never double-charge.
//   - a charge/freeze commits in the same transaction as the state change it
//     pays for: if the state change fails, the money never moves.

// lockReseller serializes all balance-affecting operations for one reseller
// so concurrent charges cannot both pass the funds check.
func lockReseller(ctx context.Context, tx dbtx, resellerID string) error {
	var id string
	return tx.GetContext(ctx, &id, `SELECT id FROM users WHERE id = $1 FOR UPDATE`, resellerID)
}

func balance(ctx context.Context, q dbtx, resellerID string) (int64, error) {
	var b int64
	err := q.GetContext(ctx, &b, `
		SELECT COALESCE(SUM(CASE WHEN kind = 'credit' THEN amount_cents ELSE -amount_cents END), 0)
		FROM ledger_entries WHERE reseller_id = $1`, resellerID)
	return b, err
}

func heldAmount(ctx context.Context, q dbtx, resellerID string) (int64, error) {
	var h int64
	err := q.GetContext(ctx, &h, `
		SELECT COALESCE(SUM(amount_cents), 0) FROM credit_holds
		WHERE reseller_id = $1 AND status = 'open'`, resellerID)
	return h, err
}

// ensureFunds locks the reseller row and verifies available balance.
func ensureFunds(ctx context.Context, tx dbtx, resellerID string, amount int64) error {
	if err := lockReseller(ctx, tx, resellerID); err != nil {
		return err
	}
	b, err := balance(ctx, tx, resellerID)
	if err != nil {
		return err
	}
	h, err := heldAmount(ctx, tx, resellerID)
	if err != nil {
		return err
	}
	if b-h < amount {
		return fmt.Errorf("%w: need %d cents, available %d", ErrInsufficientCredits, amount, b-h)
	}
	return nil
}

// charge appends a charge row. Idempotent by key.
func charge(ctx context.Context, tx dbtx, resellerID string, amount int64, key, memo, domainName string, transferID *string) error {
	if amount <= 0 {
		return nil
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO ledger_entries (reseller_id, idempotency_key, kind, amount_cents, memo, domain_name, transfer_id)
		VALUES ($1, $2, 'charge', $3, $4, NULLIF($5, ''), $6)
		ON CONFLICT (idempotency_key) DO NOTHING`,
		resellerID, key, amount, memo, domainName, transferID)
	return err
}

// credit appends a credit row (top-up). Idempotent by key.
func credit(ctx context.Context, tx dbtx, resellerID string, amount int64, key, memo string) error {
	if amount <= 0 {
		return ErrValidation("amount_cents must be positive")
	}
	_, err := tx.ExecContext(ctx, `
		INSERT INTO ledger_entries (reseller_id, idempotency_key, kind, amount_cents, memo)
		VALUES ($1, $2, 'credit', $3, $4)
		ON CONFLICT (idempotency_key) DO NOTHING`, resellerID, key, amount, memo)
	return err
}

// freeze creates an open hold. Idempotent by key.
func freeze(ctx context.Context, tx dbtx, holdID, resellerID string, amount int64, key string, transferID *string) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO credit_holds (id, reseller_id, amount_cents, status, idempotency_key, transfer_id)
		VALUES ($1, $2, $3, 'open', $4, $5)
		ON CONFLICT (idempotency_key) DO NOTHING`, holdID, resellerID, amount, key, transferID)
	return err
}

// releaseHold opens a hold back up (cancel / reject / timeout). Conditional
// on status='open' so it is safe to run twice.
func releaseHold(ctx context.Context, tx dbtx, holdID string) error {
	_, err := tx.ExecContext(ctx,
		`UPDATE credit_holds SET status = 'released', updated_at = now() WHERE id = $1 AND status = 'open'`, holdID)
	return err
}

// captureHold converts a hold into a real charge. Both steps are conditional
// / conflict-guarded, so a retried completion charges exactly once.
func captureHold(ctx context.Context, tx dbtx, holdID, resellerID string, amount int64, key, memo, domainName string, transferID *string) error {
	res, err := tx.ExecContext(ctx,
		`UPDATE credit_holds SET status = 'captured', updated_at = now() WHERE id = $1 AND status = 'open'`, holdID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return nil // already captured or released
	}
	return charge(ctx, tx, resellerID, amount, key, memo, domainName, transferID)
}
