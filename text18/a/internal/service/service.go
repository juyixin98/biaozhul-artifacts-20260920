// Package service contains the SIRCC business logic. Every state-changing
// operation runs in a single database transaction so that incident status,
// phase records and audit events can never be half-updated.
package service

import (
	"context"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/clock"
	"sircc/internal/domain"
	"sircc/internal/store"
)

type Service struct {
	pool  *pgxpool.Pool
	clock clock.Clock
}

func New(pool *pgxpool.Pool, c clock.Clock) *Service {
	if c == nil {
		c = clock.Real{}
	}
	return &Service{pool: pool, clock: c}
}

// inTx runs fn inside a transaction, rolling back on any error.
func (s *Service) inTx(ctx context.Context, fn func(q *store.Queries) error) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err := fn(store.New(tx)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// actor bundles the authenticated caller with the case-role lookup.
type actor struct {
	user *store.User
}

// requireCaseAccess verifies the caller may touch the incident and (when
// caseRoles is non-empty) holds one of the case roles. Global admins bypass
// both checks. Callers should already hold the incident row FOR UPDATE when
// they are about to mutate it.
func (s *Service) requireCaseAccess(ctx context.Context, q *store.Queries,
	incidentID uuid.UUID, u *store.User, caseRoles ...string,
) (*store.IncidentMember, error) {
	if u.Role == domain.RoleAdmin {
		return &store.IncidentMember{IncidentID: incidentID, UserID: u.ID, CaseRole: domain.RoleAdmin}, nil
	}
	member, err := q.GetMember(ctx, store.GetMemberParams{IncidentID: incidentID, UserID: u.ID})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, domain.ErrForbidden
		}
		return nil, err
	}
	if len(caseRoles) == 0 {
		return &member, nil
	}
	for _, r := range caseRoles {
		if member.CaseRole == r {
			return &member, nil
		}
	}
	return nil, domain.ErrForbidden
}
