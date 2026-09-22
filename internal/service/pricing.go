package service

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// priceAt returns the price in effect at time `at` for (tld, action),
// falling back to the wildcard TLD "*". Prices are historical rows keyed by
// effective_from; accepted transactions copy the amount into their own
// records (ledger entries, transfers.price_cents), so a later price change
// never rewrites an accepted transaction.
func (s *Service) priceAt(ctx context.Context, tld, action string, at time.Time) (int64, error) {
	var amount int64
	err := s.db.GetContext(ctx, &amount, `
		SELECT amount_cents FROM prices
		WHERE tld = $1 AND action = $2 AND effective_from <= $3
		ORDER BY effective_from DESC, id DESC LIMIT 1`, tld, action, at)
	if errors.Is(err, sql.ErrNoRows) && tld != "*" {
		return s.priceAt(ctx, "*", action, at)
	}
	if errors.Is(err, sql.ErrNoRows) {
		return 0, ErrNoPrice
	}
	return amount, err
}

// SetPrice appends a new price row (admin). Existing rows are never updated,
// so the price history used by already-accepted transactions stays intact.
func (s *Service) SetPrice(ctx context.Context, tld, action string, amountCents int64, effectiveFrom *time.Time) error {
	switch action {
	case ActionRegister, ActionRenew, ActionRedeem, ActionTransfer:
	default:
		return ErrValidation("unknown price action %q", action)
	}
	if amountCents < 0 {
		return ErrValidation("amount_cents must be >= 0")
	}
	at := s.clock.Now()
	if effectiveFrom != nil {
		at = effectiveFrom.UTC()
	}
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO prices (tld, action, amount_cents, effective_from) VALUES ($1, $2, $3, $4)`,
		tld, action, amountCents, at)
	return err
}

type Price struct {
	TLD           string    `db:"tld" json:"tld"`
	Action        string    `db:"action" json:"action"`
	AmountCents   int64     `db:"amount_cents" json:"amount_cents"`
	EffectiveFrom time.Time `db:"effective_from" json:"effective_from"`
}

func (s *Service) CurrentPrices(ctx context.Context) ([]Price, error) {
	var out []Price
	err := s.db.SelectContext(ctx, &out, `
		SELECT DISTINCT ON (tld, action) tld, action, amount_cents, effective_from
		FROM prices WHERE effective_from <= $1
		ORDER BY tld, action, effective_from DESC, id DESC`, s.clock.Now())
	return out, err
}
