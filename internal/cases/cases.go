// Package cases manages case registration.
package cases

import (
	"context"
	"errors"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/domain"
	"forensiccore/internal/hashing"
	"forensiccore/internal/idutil"

	"gorm.io/gorm"
)

// ErrNotFound is returned when a case id is unknown.
var ErrNotFound = errors.New("case not found")

// ErrInvalidID is returned for client supplied ids that fail validation.
var ErrInvalidID = errors.New("case id must match [A-Za-z0-9_-]{1,64}")

// Service handles case persistence.
type Service struct {
	db     *gorm.DB
	events *chain.Appender
}

// New builds a cases service.
func New(db *gorm.DB, events *chain.Appender) *Service {
	return &Service{db: db, events: events}
}

// CreateInput is the request body for case creation.
type CreateInput struct {
	ID          string `json:"id"`
	Name        string `json:"name" binding:"required"`
	Description string `json:"description"`
	Actor       string `json:"-"`
}

// Create persists a case and its case_created genesis event atomically.
func (s *Service) Create(ctx context.Context, in CreateInput) (*domain.Case, *domain.ChainEvent, error) {
	if in.ID != "" && !idutil.ValidExternalID(in.ID) {
		return nil, nil, ErrInvalidID
	}
	id := in.ID
	if id == "" {
		id = idutil.New()
	}
	kase := domain.Case{
		ID:          id,
		Name:        in.Name,
		Description: in.Description,
		GenesisHash: hashing.GenesisHash(id),
	}
	var chainEv *domain.ChainEvent
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&kase).Error; err != nil {
			return err
		}
		ev, err := s.events.AppendOn(ctx, tx, chain.AppendInput{
			CaseID:    kase.ID,
			EventType: domain.EventCaseCreated,
			Actor:     in.Actor,
			Payload: hashing.CaseCreatedPayload{
				Name:        kase.Name,
				Description: kase.Description,
			},
			Occurred: time.Now().UTC(),
		})
		if err != nil {
			return err
		}
		chainEv = ev
		return nil
	})
	if err != nil {
		if chain.IsDuplicateKeyErr(err) {
			return nil, nil, errors.New("case id already exists")
		}
		return nil, nil, err
	}
	return &kase, chainEv, nil
}

// Get fetches a case.
func (s *Service) Get(ctx context.Context, id string) (*domain.Case, error) {
	var kase domain.Case
	if err := s.db.WithContext(ctx).First(&kase, "id = ?", id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &kase, nil
}

// List returns all cases newest first.
func (s *Service) List(ctx context.Context) ([]domain.Case, error) {
	var out []domain.Case
	err := s.db.WithContext(ctx).Order("created_at DESC").Find(&out).Error
	return out, err
}
