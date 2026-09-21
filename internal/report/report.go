// Package report renders a complete case export: baseline, reviews and the
// full hash chain with a fresh verification result.
package report

import (
	"context"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/domain"

	"gorm.io/gorm"
)

// Disclaimer is embedded in every report. A hash chain provides integrity
// only: it cannot establish when events really happened (no trusted time) and
// it is not an external digital signature.
const Disclaimer = `哈希链仅提供完整性检查：它可以发现链上事件的缺失、篡改与乱序，但不能证明事件发生的真实时间（无可信时间戳），也不能代替外部数字签名或司法合规认证。The hash chain provides integrity detection only: it cannot establish trusted timestamps, nor does it replace external digital signatures or forensic/judicial compliance certification.`

// Report is the exported case bundle.
type Report struct {
	GeneratedAt  time.Time           `json:"generated_at"`
	Case         domain.Case         `json:"case"`
	Evidence     []domain.Evidence   `json:"evidence"`
	Reviews      []domain.ReviewJob  `json:"reviews"`
	Chain        []domain.ChainEvent `json:"chain"`
	Verification *chain.VerifyReport `json:"verification"`
	Disclaimer   string              `json:"disclaimer"`
}

// Service builds reports.
type Service struct {
	db     *gorm.DB
	events *chain.Appender
}

// New builds a report service.
func New(db *gorm.DB, events *chain.Appender) *Service {
	return &Service{db: db, events: events}
}

// Build assembles the export for one case.
func (s *Service) Build(ctx context.Context, caseID string) (*Report, error) {
	var kase domain.Case
	if err := s.db.WithContext(ctx).First(&kase, "id = ?", caseID).Error; err != nil {
		return nil, err
	}
	var evs []domain.Evidence
	if err := s.db.WithContext(ctx).Where("case_id = ?", caseID).Order("created_at ASC").Find(&evs).Error; err != nil {
		return nil, err
	}
	var jobs []domain.ReviewJob
	if err := s.db.WithContext(ctx).Where("case_id = ?", caseID).Order("created_at ASC").Find(&jobs).Error; err != nil {
		return nil, err
	}
	chainEvents, err := s.events.ListEvents(ctx, caseID)
	if err != nil {
		return nil, err
	}
	verification, err := s.events.Verify(ctx, caseID)
	if err != nil {
		return nil, err
	}
	return &Report{
		GeneratedAt:  time.Now().UTC(),
		Case:         kase,
		Evidence:     evs,
		Reviews:      jobs,
		Chain:        chainEvents,
		Verification: verification,
		Disclaimer:   Disclaimer,
	}, nil
}
