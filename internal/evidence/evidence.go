// Package evidence registers raw/dd images and records their baselines.
package evidence

import (
	"context"
	"errors"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/domain"
	"forensiccore/internal/hashing"
	"forensiccore/internal/idutil"
	"forensiccore/internal/securefile"

	"gorm.io/gorm"
)

// Sentinel errors.
var (
	ErrCaseNotFound  = errors.New("case not found")
	ErrAlreadyExists = errors.New("evidence for this source path already registered in the case")
	ErrNotFound      = errors.New("evidence not found")
)

// Service handles evidence registration and custody bookkeeping.
type Service struct {
	db        *gorm.DB
	resolver  *securefile.Resolver
	events    *chain.Appender
	chunkSize int

	// RegisterHook, if non-nil, is invoked after every chunk during both
	// hashing passes. Production leaves it nil; tests use it to mutate files
	// mid-read.
	RegisterHook securefile.ChunkHook

	// hasher opens the per-file hash routine. Tests can set it to inject a
	// ChunkHook; New installs the production no-hook hasher.
	hasher func(f *securefile.File) (string, error)
}

// New builds a Service.
func New(db *gorm.DB, resolver *securefile.Resolver, events *chain.Appender, chunkSize int) *Service {
	s := &Service{db: db, resolver: resolver, events: events, chunkSize: chunkSize}
	s.hasher = func(f *securefile.File) (string, error) {
		return f.HashAndVerify(chunkSize, s.RegisterHook)
	}
	return s
}

// RegisterInput is the registration request.
type RegisterInput struct {
	CaseID     string `json:"case_id"`
	SourcePath string `json:"source_path" binding:"required"`
	Actor      string `json:"-"`
}

// Register verifies and registers one image.
//
// The file is hashed TWICE against a read-only descriptor. Before each pass
// and after every pass the descriptor identity (dev/inode/size/mtime) must be
// identical; otherwise registration fails with securefile.ErrFileChanged and
// NO baseline row is persisted. This makes a "wrong baseline saved from a
// moving file" outcome impossible rather than merely detectable.
func (s *Service) Register(ctx context.Context, in RegisterInput) (*domain.Evidence, *domain.ChainEvent, error) {
	var kase domain.Case
	if err := s.db.WithContext(ctx).First(&kase, "id = ?", in.CaseID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil, ErrCaseNotFound
		}
		return nil, nil, err
	}

	f, err := s.resolver.Resolve(in.SourcePath)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()
	id := f.Identity()

	// One evidence per (case, resolved path): prevents double registration.
	var existing int64
	if err := s.db.Model(&domain.Evidence{}).
		Where("case_id = ? AND real_path = ?", in.CaseID, id.RealPath).
		Count(&existing).Error; err != nil {
		return nil, nil, err
	}
	if existing > 0 {
		return nil, nil, ErrAlreadyExists
	}

	first, err := s.hasher(f)
	if err != nil {
		return nil, nil, err
	}
	// Second independent pass: the same open descriptor is re-read; identity
	// must still match. This catches a replacement that happens to share size
	// and the narrow window where mtime did not tick.
	second, err := s.hasher(f)
	if err != nil {
		return nil, nil, err
	}
	if first != second {
		return nil, nil, securefile.ErrFileChanged
	}

	evRow := domain.Evidence{
		ID:           idutil.New(),
		CaseID:       in.CaseID,
		SourcePath:   in.SourcePath,
		RealPath:     id.RealPath,
		Filename:     baseName(id.RealPath),
		Size:         id.Size,
		SHA256:       first,
		FileMode:     uint32(id.Mode.Perm()),
		DeviceID:     id.DeviceID,
		Inode:        id.Inode,
		Custodian:    in.Actor,
		RegisteredBy: in.Actor,
	}

	var chainEv *domain.ChainEvent
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(&evRow).Error; err != nil {
			return err
		}
		ev, err := s.events.AppendOn(ctx, tx, chain.AppendInput{
			CaseID:    in.CaseID,
			EventType: domain.EventRegistered,
			Actor:     in.Actor,
			Payload: hashing.RegisteredPayload{
				EvidenceID: evRow.ID,
				SourcePath: evRow.SourcePath,
				RealPath:   evRow.RealPath,
				Filename:   evRow.Filename,
				Size:       evRow.Size,
				SHA256:     evRow.SHA256,
			},
			Occurred: time.Now().UTC(),
		})
		if err != nil {
			return err
		}
		chainEv = ev
		if err := tx.Model(&domain.Evidence{}).Where("id = ?", evRow.ID).
			Update("registered_seq", ev.Seq).Error; err != nil {
			return err
		}
		evRow.RegisteredSeq = ev.Seq
		return nil
	})
	if err != nil {
		if chain.IsDuplicateKeyErr(err) {
			return nil, nil, ErrAlreadyExists
		}
		return nil, nil, err
	}
	return &evRow, chainEv, nil
}

// Get fetches one evidence row.
func (s *Service) Get(ctx context.Context, id string) (*domain.Evidence, error) {
	var ev domain.Evidence
	if err := s.db.WithContext(ctx).First(&ev, "id = ?", id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &ev, nil
}

// ListByCase lists evidence for a case.
func (s *Service) ListByCase(ctx context.Context, caseID string) ([]domain.Evidence, error) {
	var out []domain.Evidence
	err := s.db.WithContext(ctx).Where("case_id = ?", caseID).
		Order("created_at ASC").Find(&out).Error
	return out, err
}

// UpdateCustodian changes the current custodian (used during transfer).
func (s *Service) UpdateCustodian(ctx context.Context, tx *gorm.DB, evidenceID, custodian string) error {
	return tx.WithContext(ctx).Model(&domain.Evidence{}).
		Where("id = ?", evidenceID).Update("custodian", custodian).Error
}

func baseName(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' {
			return p[i+1:]
		}
	}
	return p
}
