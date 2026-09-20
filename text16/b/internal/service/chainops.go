package service

import (
	"strings"
	"time"

	"gorm.io/gorm"

	"github.com/example/forensiccore/internal/chain"
	"github.com/example/forensiccore/internal/model"
)

// TransferInput 移交入参。
type TransferInput struct {
	CaseID     uint
	EvidenceID uint
	To         string
	Reason     string
	Actor      string
}

// Transfer 变更案件保管人并追加移交事件（调查员）。
func (s *Service) Transfer(in TransferInput) (*model.ChainEvent, error) {
	ev, err := s.GetEvidence(in.CaseID, in.EvidenceID)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(in.To) == "" {
		return nil, kindErr(KindValidation, "transfer target (to) is required")
	}
	c, err := s.GetCase(in.CaseID)
	if err != nil {
		return nil, err
	}

	from := c.Custodian
	var created *model.ChainEvent
	err = s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Model(&model.Case{}).Where("id = ?", in.CaseID).
			Update("custodian", strings.TrimSpace(in.To)).Error; err != nil {
			return err
		}
		ce, err := chain.Append(tx, chain.AppendInput{
			CaseID: in.CaseID,
			Type:   model.EventTransfer,
			Actor:  in.Actor,
			Payload: mustJSON(TransferPayload{
				EvidenceID: ev.ID,
				From:       from,
				To:         strings.TrimSpace(in.To),
				Reason:     strings.TrimSpace(in.Reason),
			}),
		})
		if err != nil {
			return err
		}
		created = ce
		return nil
	})
	if err != nil {
		return nil, err
	}
	return created, nil
}

// NoteInput 备注入参。分析师与调查员均可。
type NoteInput struct {
	CaseID     uint
	EvidenceID uint // 可选：0 表示案件级备注
	Text       string
	Actor      string
}

// AddNote 追加备注事件。
func (s *Service) AddNote(in NoteInput) (*model.ChainEvent, error) {
	if strings.TrimSpace(in.Text) == "" {
		return nil, kindErr(KindValidation, "note text is required")
	}
	if _, err := s.GetCase(in.CaseID); err != nil {
		return nil, err
	}
	if in.EvidenceID != 0 {
		if _, err := s.GetEvidence(in.CaseID, in.EvidenceID); err != nil {
			return nil, err
		}
	}
	ce, err := chain.Append(s.db, chain.AppendInput{
		CaseID: in.CaseID,
		Type:   model.EventNote,
		Actor:  in.Actor,
		At:     time.Now().UTC(),
		Payload: mustJSON(NotePayload{
			EvidenceID: in.EvidenceID,
			Text:       strings.TrimSpace(in.Text),
		}),
	})
	if err != nil {
		return nil, err
	}
	return ce, nil
}

// ListChain 按序号升序返回案件完整证据链。
func (s *Service) ListChain(caseID uint) ([]model.ChainEvent, error) {
	if _, err := s.GetCase(caseID); err != nil {
		return nil, err
	}
	var events []model.ChainEvent
	if err := s.db.Where("case_id = ?", caseID).
		Order("sequence ASC").Find(&events).Error; err != nil {
		return nil, err
	}
	return events, nil
}

// VerifyChain 校验案件链的缺失、篡改与乱序。
func (s *Service) VerifyChain(caseID uint) (*chain.Report, error) {
	if _, err := s.GetCase(caseID); err != nil {
		return nil, err
	}
	return chain.Verify(s.db, caseID)
}
