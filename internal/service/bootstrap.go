package service

import (
	"context"
	"errors"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/db"
)

// BootstrapAdmin creates the first platform admin key directly from a chosen
// secret. It fails if any admin key already exists, so it can only bootstrap a
// fresh system.
func (s *Service) BootstrapAdmin(ctx context.Context, plain string) (db.ApiKey, error) {
	var rec db.ApiKey
	err := s.inTx(ctx, func(q *db.Queries) error {
		rows, err := q.ListAPIKeys(ctx, (*uuid.UUID)(nil))
		if err != nil {
			return err
		}
		for _, k := range rows {
			if k.Role == "admin" {
				return errors.New("an admin key already exists")
			}
		}
		if err := ensureInternalAccounts(ctx, q); err != nil {
			return err
		}
		rec, err = q.CreateAPIKey(ctx, db.CreateAPIKeyParams{
			KeyHash:   HashAPIKey(plain),
			KeyPrefix: plainPrefix(plain),
			Role:      "admin",
			Label:     "bootstrap admin",
		})
		return err
	})
	return rec, err
}

// plainPrefix returns up to the first 12 characters for masked display.
func plainPrefix(plain string) string {
	if len(plain) <= 12 {
		return plain
	}
	return plain[:12]
}
