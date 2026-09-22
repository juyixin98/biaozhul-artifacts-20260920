// Package ledger implements the reseller credit ledger.
//
// All amounts are integer cents. Every money movement is one
// billing_transactions row plus a ledger_entries row, written in the same
// local transaction as the state change that charges it — there is no window
// where a domain is registered without being paid, or money is taken without a
// state change. On any failure the transaction rolls back and nothing is
// charged.
//
// Each reseller has two pools:
//
//	available : money the reseller can spend right now
//	held      : money frozen by an in-flight transfer
//
// Movements (all integer cents):
//
//	topup            available +X
//	register/renew   available -X
//	restore          available -X
//	transfer_freeze  available -N, held +N
//	transfer_release available +N, held -N
//	transfer_capture available  0, held -N   (frozen money is consumed)
//
// Idempotency: requests carrying an idempotency key take a transaction-scoped
// advisory lock keyed on (reseller, kind, key) before doing anything, so
// concurrent retries serialize: the first posts the transaction, the rest
// return the original result instead of charging twice.
package ledger

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"errors"
	"fmt"

	"domainengine/internal/apierror"
	"domainengine/internal/models"
	"domainengine/internal/store"

	"github.com/jmoiron/sqlx"
)

type Service struct {
	db *sqlx.DB
}

func New(db *sqlx.DB) *Service { return &Service{db: db} }

// Post describes one ledger movement. AvailableDelta and HeldDelta are signed
// deltas applied to the two pools; they must keep both pools non-negative.
type Post struct {
	ResellerID     int64
	Kind           string
	AvailableDelta int64
	HeldDelta      int64
	DomainID       *int64
	TransferID     *int64
	Years          *int
	IdempotencyKey *string
}

// AmountCents is stored for display: the magnitude of money this posting moved.
func (p Post) AmountCents() int64 {
	switch {
	case p.AvailableDelta != 0:
		return p.AvailableDelta // signed (topup > 0, charges < 0)
	case p.HeldDelta < 0:
		return p.HeldDelta // capture/release consume held
	default:
		return p.HeldDelta // freeze
	}
}

func keyHash(resellerID int64, kind, key string) uint64 {
	h := sha256.Sum256([]byte(fmt.Sprintf("%d|%s|%s", resellerID, kind, key)))
	return binary.BigEndian.Uint64(h[:8])
}

// lockIdempotency serializes concurrent identical requests for the lifetime of
// the surrounding transaction.
func lockIdempotency(ctx context.Context, tx *sqlx.Tx, resellerID int64, kind string, key *string) error {
	if key == nil || *key == "" {
		return nil
	}
	return LockIdempotency(ctx, tx, resellerID, kind, *key)
}

// LockIdempotency takes the transaction-scoped advisory lock used to serialize
// idempotent retries. Services that need to check "already done?" *before*
// mutating other rows call this first so racing retries queue up.
func LockIdempotency(ctx context.Context, tx *sqlx.Tx, resellerID int64, kind, key string) error {
	if key == "" {
		return nil
	}
	id := int64(keyHash(resellerID, kind, key) & 0x7fffffffffffffff)
	_, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, id)
	return err
}

// FindTx looks up a posted transaction by idempotency key inside a tx.
func FindTx(ctx context.Context, tx *sqlx.Tx, resellerID int64, kind string, key *string) (*models.BillingTransaction, error) {
	if key == nil || *key == "" {
		return nil, nil
	}
	var t models.BillingTransaction
	err := tx.GetContext(ctx, &t,
		`SELECT * FROM billing_transactions
		 WHERE reseller_id=$1 AND kind=$2 AND idempotency_key=$3`,
		resellerID, kind, *key)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &t, nil
}

// FindByKey returns a previously posted transaction for an idempotency key, or
// nil when none exists. Used to return the original result for retries.
func (s *Service) FindByKey(ctx context.Context, resellerID int64, kind, key string) (*models.BillingTransaction, error) {
	var t models.BillingTransaction
	err := s.db.GetContext(ctx, &t,
		`SELECT * FROM billing_transactions
		 WHERE reseller_id=$1 AND kind=$2 AND idempotency_key=$3`,
		resellerID, kind, key)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &t, nil
}

// PostTx applies a ledger movement inside an existing transaction. The caller
// commits together with its own state changes. CHECK constraints plus the
// FOR UPDATE lock here prevent negative balances under concurrency.
func (s *Service) PostTx(ctx context.Context, tx *sqlx.Tx, p Post) (*models.BillingTransaction, error) {
	if err := lockIdempotency(ctx, tx, p.ResellerID, p.Kind, p.IdempotencyKey); err != nil {
		return nil, err
	}
	if p.IdempotencyKey != nil && *p.IdempotencyKey != "" {
		var existing models.BillingTransaction
		err := tx.GetContext(ctx, &existing,
			`SELECT * FROM billing_transactions
			 WHERE reseller_id=$1 AND kind=$2 AND idempotency_key=$3`,
			p.ResellerID, p.Kind, *p.IdempotencyKey)
		if err == nil {
			return &existing, nil
		} else if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
	}

	// Lock the reseller row so balance math is race-free inside the tx.
	var r models.Reseller
	if err := tx.GetContext(ctx, &r,
		`SELECT * FROM resellers WHERE id=$1 FOR UPDATE`, p.ResellerID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, apierror.ErrNotFound
		}
		return nil, err
	}
	newAvailable := r.BalanceCents + p.AvailableDelta
	newHeld := r.HeldCents + p.HeldDelta
	if newAvailable < 0 {
		return nil, apierror.ErrInsufficientFunds
	}
	if newHeld < 0 {
		return nil, fmt.Errorf("ledger: held balance would go negative (%d + %d)", r.HeldCents, p.HeldDelta)
	}

	var bt models.BillingTransaction
	err := tx.QueryRowxContext(ctx, `
		INSERT INTO billing_transactions
			(reseller_id, kind, amount_cents, status, domain_id, transfer_id, years, idempotency_key)
		VALUES ($1,$2,$3,'completed',$4,$5,$6,$7)
		RETURNING *`,
		p.ResellerID, p.Kind, p.AmountCents(), p.DomainID, p.TransferID,
		p.Years, p.IdempotencyKey).
		StructScan(&bt)
	if err != nil {
		if store.IsUniqueViolation(err) && p.IdempotencyKey != nil {
			// A racing retry won the insert; return its row as the original.
			var existing models.BillingTransaction
			if gErr := tx.GetContext(ctx, &existing,
				`SELECT * FROM billing_transactions
				 WHERE reseller_id=$1 AND kind=$2 AND idempotency_key=$3`,
				p.ResellerID, p.Kind, *p.IdempotencyKey); gErr == nil {
				return &existing, nil
			}
			return nil, err
		}
		return nil, err
	}

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO ledger_entries
			(tx_id, reseller_id, available_delta, held_delta, after_available, after_held)
		VALUES ($1,$2,$3,$4,$5,$6)`,
		bt.ID, p.ResellerID, p.AvailableDelta, p.HeldDelta,
		newAvailable, newHeld); err != nil {
		return nil, err
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE resellers SET balance_cents=$1, held_cents=$2 WHERE id=$3`,
		newAvailable, newHeld, p.ResellerID); err != nil {
		return nil, err
	}
	return &bt, nil
}

// Topup credits a reseller's available balance with its own transaction
// (idempotent when key is supplied).
func (s *Service) Topup(ctx context.Context, resellerID, amount int64, key *string) (*models.BillingTransaction, error) {
	if amount <= 0 {
		return nil, apierror.ErrBadRequest
	}
	var out *models.BillingTransaction
	err := store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		bt, err := s.PostTx(ctx, tx, Post{
			ResellerID:     resellerID,
			Kind:           models.TxTopup,
			AvailableDelta: amount,
			IdempotencyKey: key,
		})
		if err != nil {
			return err
		}
		out = bt
		return nil
	})
	return out, err
}

// History lists a reseller's transactions (newest first), reseller-scoped.
func (s *Service) History(ctx context.Context, resellerID int64, limit int) ([]models.BillingTransaction, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	var out []models.BillingTransaction
	err := s.db.SelectContext(ctx, &out,
		`SELECT * FROM billing_transactions WHERE reseller_id=$1
		 ORDER BY id DESC LIMIT $2`, resellerID, limit)
	return out, err
}
