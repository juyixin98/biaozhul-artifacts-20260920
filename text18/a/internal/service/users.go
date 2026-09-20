package service

import (
	"context"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/domain"
	"sircc/internal/store"
)

var _ = uuid.Nil

// ListUsers returns the active user directory.
func (s *Service) ListUsers(ctx context.Context, _ *store.User) ([]UserView, error) {
	rows, err := store.New(s.pool).ListUsers(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]UserView, 0, len(rows))
	for _, u := range rows {
		out = append(out, userView(u))
	}
	return out, nil
}

// ListMembers returns the people assigned to a case.
func (s *Service) ListMembers(ctx context.Context, caller *store.User, incidentID uuid.UUID) ([]MemberView, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	q := store.New(tx)
	if _, err := q.GetIncident(ctx, incidentID); err != nil {
		if err == pgx.ErrNoRows {
			return nil, domain.ErrNotFound
		}
		return nil, err
	}
	if _, err := s.requireCaseAccess(ctx, q, incidentID, caller); err != nil {
		return nil, err
	}
	rows, err := q.ListMembers(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	out := make([]MemberView, 0, len(rows))
	for _, m := range rows {
		mv := MemberView{
			UserID:    m.UserID.String(),
			Username:  m.Username,
			FullName:  m.FullName,
			CaseRole:  m.CaseRole,
			CreatedAt: ts(m.CreatedAt),
		}
		if m.AssignedBy.Valid {
			mv.AssignedBy = strPtr(uuid.UUID(m.AssignedBy.Bytes).String())
		}
		out = append(out, mv)
	}
	return out, tx.Commit(ctx)
}
