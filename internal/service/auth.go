package service

import (
	"context"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/clearsettle/clearsettle/internal/domain"
)

// Authenticate resolves a plaintext API key to its actor.
func (s *Service) Authenticate(ctx context.Context, plain string) (Actor, error) {
	if plain == "" {
		return Actor{}, domain.ErrForbidden
	}
	rec, err := s.q.GetAPIKeyByHash(ctx, HashAPIKey(plain))
	if err != nil {
		if err == pgx.ErrNoRows {
			return Actor{}, domain.ErrForbidden
		}
		return Actor{}, err
	}
	return Actor{
		KeyID:      rec.ID,
		Role:       rec.Role,
		MerchantID: rec.MerchantID,
	}, nil
}

// APIKeyView is the masked representation of a stored key.
type APIKeyView struct {
	ID         string  `json:"id"`
	Prefix     string  `json:"prefix"`
	Role       string  `json:"role"`
	MerchantID *string `json:"merchant_id"`
	Label      string  `json:"label"`
	Active     bool    `json:"active"`
}

// ListAPIKeys lists keys masked. Only admins may call it; the secret is never
// returned.
func (s *Service) ListAPIKeys(ctx context.Context, actor Actor) ([]APIKeyView, error) {
	if actor.Role != "admin" {
		return nil, domain.ErrForbidden
	}
	rows, err := s.q.ListAPIKeys(ctx, (*uuid.UUID)(nil))
	if err != nil {
		return nil, err
	}
	out := make([]APIKeyView, 0, len(rows))
	for _, r := range rows {
		v := APIKeyView{
			ID:     r.ID.String(),
			Prefix: MaskAPIKey(r.KeyPrefix),
			Role:   r.Role,
			Label:  r.Label,
			Active: r.Active,
		}
		if r.MerchantID != nil {
			m := r.MerchantID.String()
			v.MerchantID = &m
		}
		out = append(out, v)
	}
	return out, nil
}
