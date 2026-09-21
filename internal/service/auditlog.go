package service

import (
	"context"

	"github.com/google/uuid"

	"github.com/clearsettle/clearsettle/internal/db"
	"github.com/clearsettle/clearsettle/internal/domain"
)

// ListActiveMerchantIDs returns all active merchant ids (batch worker).
func (s *Service) ListActiveMerchantIDs(ctx context.Context) ([]uuid.UUID, error) {
	return s.q.ListMerchantIDs(ctx)
}

// AuditEventView is a masked audit log entry.
type AuditEventView struct {
	ID         int64   `json:"id"`
	ActorKeyID *string `json:"actor_key_id"`
	ActorRole  string  `json:"actor_role"`
	MerchantID *string `json:"merchant_id"`
	Action     string  `json:"action"`
	TargetType string  `json:"target_type"`
	TargetID   string  `json:"target_id"`
	Metadata   []byte  `json:"metadata"`
	CreatedAt  string  `json:"created_at"`
}

// ListAuditEvents returns audit entries. Admins see the whole platform log;
// operators/auditors see only their merchant's entries.
func (s *Service) ListAuditEvents(ctx context.Context, actor Actor,
	merchantID *uuid.UUID, limit, offset int32) ([]AuditEventView, error) {
	if actor.Role != "admin" {
		if actor.MerchantID == nil {
			return nil, domain.ErrForbidden
		}
		m := *actor.MerchantID
		merchantID = &m
	}
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	rows, err := s.q.ListAuditEvents(ctx, db.ListAuditEventsParams{
		MerchantID: merchantID, Limit: limit, Offset: offset,
	})
	if err != nil {
		return nil, err
	}
	out := make([]AuditEventView, 0, len(rows))
	for _, r := range rows {
		v := AuditEventView{
			ID:         r.ID,
			ActorRole:  r.ActorRole,
			Action:     r.Action,
			TargetType: r.TargetType,
			TargetID:   r.TargetID,
			Metadata:   r.Metadata,
			CreatedAt:  r.CreatedAt.Time.Format("2006-01-02T15:04:05Z07:00"),
		}
		if r.ActorKeyID != nil {
			k := r.ActorKeyID.String()
			v.ActorKeyID = &k
		}
		if r.MerchantID != nil {
			m := r.MerchantID.String()
			v.MerchantID = &m
		}
		out = append(out, v)
	}
	return out, nil
}

// GetReconRun returns a run plus its discrepancies.
func (s *Service) GetReconRun(ctx context.Context, actor Actor,
	merchantID uuid.UUID, runDate string) (db.ReconciliationRun, []db.Discrepancy, error) {
	if err := actor.requireMerchant(merchantID); err != nil {
		return db.ReconciliationRun{}, nil, err
	}
	run, err := s.q.GetReconRun(ctx, db.GetReconRunParams{MerchantID: merchantID, RunDate: parseDate(runDate)})
	if err != nil {
		if isNoRows(err) {
			return db.ReconciliationRun{}, nil, domain.ErrNotFound
		}
		return db.ReconciliationRun{}, nil, err
	}
	disc, err := s.q.ListDiscrepancies(ctx, run.ID)
	if err != nil {
		return db.ReconciliationRun{}, nil, err
	}
	return run, disc, nil
}
