package evidence

import (
	"context"
	"errors"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/domain"
	"forensiccore/internal/hashing"

	"gorm.io/gorm"
)

// TransferInput moves custody of one evidence item.
type TransferInput struct {
	CaseID      string `json:"case_id"`
	EvidenceID  string `json:"evidence_id"` // populated from the URL path
	ToCustodian string `json:"to_custodian" binding:"required"`
	Reason      string `json:"reason" binding:"required"`
	Actor       string `json:"-"`
}

// Transfer records a transfer chain event and updates the current custodian.
// The original image file is never touched.
func (s *Service) Transfer(ctx context.Context, in TransferInput) (*domain.ChainEvent, error) {
	ev, err := s.Get(ctx, in.EvidenceID)
	if err != nil {
		return nil, err
	}
	if in.CaseID != "" && ev.CaseID != in.CaseID {
		return nil, ErrNotFound
	}

	var chainEv *domain.ChainEvent
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		created, err := s.events.AppendOn(ctx, tx, chain.AppendInput{
			CaseID:    ev.CaseID,
			EventType: domain.EventTransfer,
			Actor:     in.Actor,
			Payload: hashing.TransferPayload{
				EvidenceID:    ev.ID,
				FromCustodian: ev.Custodian,
				ToCustodian:   in.ToCustodian,
				Reason:        in.Reason,
			},
			Occurred: time.Now().UTC(),
		})
		if err != nil {
			return err
		}
		chainEv = created
		return s.UpdateCustodian(ctx, tx, ev.ID, in.ToCustodian)
	})
	if err != nil {
		return nil, err
	}
	return chainEv, nil
}

// NoteInput appends an analyst/investigator note to the chain.
type NoteInput struct {
	CaseID     string `json:"case_id"`
	EvidenceID string `json:"evidence_id"`
	Note       string `json:"note" binding:"required"`
	Actor      string `json:"-"`
}

// AddNote appends a note event. Notes never modify evidence files or baselines.
func (s *Service) AddNote(ctx context.Context, in NoteInput) (*domain.ChainEvent, error) {
	if in.CaseID == "" {
		return nil, errors.New("case_id is required")
	}
	if _, err := s.caseExists(ctx, in.CaseID); err != nil {
		return nil, err
	}
	payload := hashing.NotePayload{Note: in.Note}
	if in.EvidenceID != "" {
		ev, err := s.Get(ctx, in.EvidenceID)
		if err != nil {
			return nil, err
		}
		if ev.CaseID != in.CaseID {
			return nil, ErrNotFound
		}
		payload.EvidenceID = ev.ID
	}
	return s.events.Append(ctx, chain.AppendInput{
		CaseID:    in.CaseID,
		EventType: domain.EventNote,
		Actor:     in.Actor,
		Payload:   payload,
		Occurred:  time.Now().UTC(),
	})
}

func (s *Service) caseExists(ctx context.Context, id string) (*domain.Case, error) {
	var kase domain.Case
	if err := s.db.WithContext(ctx).First(&kase, "id = ?", id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrCaseNotFound
		}
		return nil, err
	}
	return &kase, nil
}
