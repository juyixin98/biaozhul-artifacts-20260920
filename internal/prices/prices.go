// Package prices manages the TLD price list.
//
// Only admins change prices. A change supersedes the old row inside one
// transaction and inserts a fresh current row — prices for transactions that
// were already accepted are snapshotted on their billing_transactions row and
// are never rewritten.
package prices

import (
	"context"
	"database/sql"
	"errors"
	"strings"

	"domainengine/internal/apierror"
	"domainengine/internal/models"
	"domainengine/internal/store"

	"github.com/jmoiron/sqlx"
)

type Service struct {
	db *sqlx.DB
}

func New(db *sqlx.DB) *Service { return &Service{db: db} }

// Current returns the active price for a TLD.
func (s *Service) Current(ctx context.Context, tld string) (*models.PriceRule, error) {
	var p models.PriceRule
	err := s.db.GetContext(ctx, &p,
		`SELECT * FROM price_rules WHERE tld=$1 AND NOT superseded`, strings.ToLower(tld))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrUnknownTLD
	}
	if err != nil {
		return nil, err
	}
	return &p, nil
}

// CurrentTx is the transaction-scoped variant; price is snapshotted by callers
// inside the same tx that charges it.
func (s *Service) CurrentTx(ctx context.Context, tx *sqlx.Tx, tld string) (*models.PriceRule, error) {
	var p models.PriceRule
	err := tx.GetContext(ctx, &p,
		`SELECT * FROM price_rules WHERE tld=$1 AND NOT superseded`, strings.ToLower(tld))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, apierror.ErrUnknownTLD
	}
	if err != nil {
		return nil, err
	}
	return &p, nil
}

// List returns all price rules, current first then history newest first.
func (s *Service) List(ctx context.Context) ([]models.PriceRule, error) {
	var out []models.PriceRule
	err := s.db.SelectContext(ctx, &out,
		`SELECT * FROM price_rules
		 ORDER BY superseded ASC, tld ASC, id DESC`)
	return out, err
}

// Set replaces the current price for a TLD atomically.
func (s *Service) Set(ctx context.Context, tld string, register, renew, restore, transfer int64) (*models.PriceRule, error) {
	tld = strings.ToLower(strings.TrimSpace(tld))
	if tld == "" || register < 0 || renew < 0 || restore < 0 || transfer < 0 {
		return nil, apierror.ErrBadRequest
	}
	var out models.PriceRule
	err := store.InTx(ctx, s.db, func(tx *sqlx.Tx) error {
		if _, err := tx.ExecContext(ctx,
			`UPDATE price_rules SET superseded=TRUE WHERE tld=$1 AND NOT superseded`, tld); err != nil {
			return err
		}
		return tx.QueryRowxContext(ctx, `
			INSERT INTO price_rules (tld, register_cents, renew_cents, restore_cents, transfer_cents)
			VALUES ($1,$2,$3,$4,$5) RETURNING *`,
			tld, register, renew, restore, transfer).StructScan(&out)
	})
	if err != nil {
		return nil, err
	}
	return &out, nil
}
