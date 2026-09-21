// Package service holds the application services: case registration with
// immutable baselines, role-given transfers/notes, and report assembly.
package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/jobs"
	"forensiccore/internal/models"
	"forensiccore/internal/safeio"

	"gorm.io/gorm"
)

// ErrConflict is returned when a unique constraint is violated.
var ErrConflict = errors.New("conflict")

// Service coordinates cases, the chain and verification jobs.
type Service struct {
	DB           *gorm.DB
	EvidenceRoot string
	ChunkSize    int
	// RegisterHook, when set, runs after each chunk while computing the
	// registration baseline (tests use it to mutate mid-read).
	RegisterHook safeio.AfterChunk
}

// RegisterRequest is the case-registration input.
type RegisterRequest struct {
	CaseRef string `json:"case_ref"`
	File    string `json:"file"`
}

// allowedExtensions are the only evidence image formats accepted.
var allowedExtensions = map[string]bool{".raw": true, ".dd": true}

// RegisterCase validates the whitelisted image, hashes it read-only in chunks,
// rejects it on any mid-read change, and persists the case plus its genesis
// chain event atomically. No baseline is saved when hashing fails.
func (s *Service) RegisterCase(req RegisterRequest, actor string) (*models.Case, error) {
	req.CaseRef = strings.TrimSpace(req.CaseRef)
	if req.CaseRef == "" || req.File == "" {
		return nil, fmt.Errorf("case_ref and file are required")
	}
	ext := strings.ToLower(filepathExt(req.File))
	if !allowedExtensions[ext] {
		return nil, fmt.Errorf("only .raw/.dd evidence images are accepted, got %q", ext)
	}

	f, id, err := safeio.OpenVerify(s.EvidenceRoot, req.File)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	digest, _, err := safeio.HashInChunks(f, id, safeio.HashOptions{
		ChunkSize:      s.ChunkSize,
		VerifyReadback: true,
		AfterChunk:     s.RegisterHook,
	})
	if err != nil {
		// Deliberately do not persist anything: a wrong baseline must never
		// be stored.
		return nil, err
	}

	c := &models.Case{
		CaseRef:      req.CaseRef,
		EvidenceFile: cleanRel(req.File),
		ResolvedPath: id.Path,
		Size:         id.Size,
		SHA256:       digest,
		MtimeNanos:   id.MtimeNanos,
		Device:       id.Device,
		Inode:        id.Inode,
		RegisteredBy: actor,
		RegisteredAt: time.Now().UTC(),
	}

	err = s.DB.Transaction(func(tx *gorm.DB) error {
		if err := tx.Create(c).Error; err != nil {
			if isDuplicateKey(err) {
				return fmt.Errorf("%w: case_ref or file already registered", ErrConflict)
			}
			return err
		}
		// Genesis event in the same transaction: the baseline never exists
		// without its register event and vice versa. This is the first event
		// for a brand-new case id, so the per-case lock cannot be contended.
		if _, err := chain.AppendTx(tx, chain.AppendOptions{
			CaseID: c.ID,
			Type:   models.EventRegister,
			Actor:  actor,
			Payload: map[string]any{
				"case_ref":      c.CaseRef,
				"evidence_file": c.EvidenceFile,
				"size":          c.Size,
				"sha256":        c.SHA256,
				"mtime_nanos":   c.MtimeNanos,
			},
		}); err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return c, nil
}

// TransferRequest moves custody of a case.
type TransferRequest struct {
	FromCustodian string `json:"from"`
	ToCustodian   string `json:"to"`
	Reason        string `json:"reason"`
}

// Transfer appends a custody-transfer event (investigators only).
func (s *Service) Transfer(caseID uint, req TransferRequest, actor string) (*models.ChainEvent, error) {
	if strings.TrimSpace(req.ToCustodian) == "" {
		return nil, fmt.Errorf("to is required")
	}
	var c models.Case
	if err := s.DB.First(&c, caseID).Error; err != nil {
		return nil, err
	}
	return chain.Append(s.DB, chain.AppendOptions{
		CaseID: caseID,
		Type:   models.EventTransfer,
		Actor:  actor,
		Payload: map[string]any{
			"from":   req.FromCustodian,
			"to":     req.ToCustodian,
			"reason": req.Reason,
		},
	})
}

// Note appends a free-form note (investigators and analysts may both note).
func (s *Service) Note(caseID uint, body string, actor string) (*models.ChainEvent, error) {
	if strings.TrimSpace(body) == "" {
		return nil, fmt.Errorf("note body is required")
	}
	var c models.Case
	if err := s.DB.First(&c, caseID).Error; err != nil {
		return nil, err
	}
	return chain.Append(s.DB, chain.AppendOptions{
		CaseID:  caseID,
		Type:    models.EventNote,
		Actor:   actor,
		Payload: map[string]any{"body": body},
	})
}

// StartVerification enqueues an integrity-review job.
func (s *Service) StartVerification(caseID uint, actor string, chunkSize int) (*models.VerificationJob, error) {
	var c models.Case
	if err := s.DB.First(&c, caseID).Error; err != nil {
		return nil, err
	}
	if chunkSize <= 0 {
		chunkSize = s.ChunkSize
	}
	return jobs.Enqueue(s.DB, caseID, chunkSize, actor)
}

// GetCase loads a case.
func (s *Service) GetCase(id uint) (*models.Case, error) {
	var c models.Case
	if err := s.DB.First(&c, id).Error; err != nil {
		return nil, err
	}
	return &c, nil
}

// FindCaseRef resolves a case_ref to a case.
func (s *Service) FindCaseRef(ref string) (*models.Case, error) {
	var c models.Case
	if err := s.DB.Where("case_ref = ?", ref).First(&c).Error; err != nil {
		return nil, err
	}
	return &c, nil
}

// ListCases returns all cases (newest first).
func (s *Service) ListCases() ([]models.Case, error) {
	var cs []models.Case
	err := s.DB.Order("id DESC").Find(&cs).Error
	return cs, err
}

// ListJobs returns verification jobs for a case.
func (s *Service) ListJobs(caseID uint) ([]models.VerificationJob, error) {
	var js []models.VerificationJob
	err := s.DB.Where("case_id = ?", caseID).Order("id ASC").Find(&js).Error
	return js, err
}

// VerifyChain runs the tamper/gap/order check for a case.
func (s *Service) VerifyChain(caseID uint) (chain.VerifyResult, error) {
	var c models.Case
	if err := s.DB.First(&c, caseID).Error; err != nil {
		return chain.VerifyResult{}, err
	}
	return chain.Verify(s.DB, caseID)
}

// ListEvents returns the full evidence chain for export / inspection.
func (s *Service) ListEvents(caseID uint) ([]models.ChainEvent, error) {
	return chain.List(s.DB, caseID)
}

// MarshalPayload pretty-prints an event's JSON payload.
func MarshalPayload(e models.ChainEvent) any {
	var v any
	if len(e.Payload) > 0 {
		_ = json.Unmarshal(e.Payload, &v)
	}
	return v
}

func filepathExt(name string) string {
	i := strings.LastIndex(name, ".")
	if i < 0 {
		return ""
	}
	// Extension must be on the final path component.
	base := name
	if j := strings.LastIndexAny(name, "/\\"); j >= 0 {
		base = name[j+1:]
	}
	if i < len(name)-len(base) {
		return ""
	}
	return strings.ToLower(name[i:])
}

func cleanRel(name string) string {
	return strings.ReplaceAll(name, "\\", "/")
}

// isDuplicateKey detects unique-index violations across drivers.
func isDuplicateKey(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "duplicate entry") || strings.Contains(msg, "duplicate key") ||
		strings.Contains(msg, "unique constraint")
}
