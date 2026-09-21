// Package report assembles the exportable case report: the registration
// baseline, all verification results and the full evidence chain.
package report

import (
	"encoding/json"
	"time"

	"forensiccore/internal/chain"
	"forensiccore/internal/models"

	"gorm.io/gorm"
)

func jsonUnmarshal(b []byte, v any) error { return json.Unmarshal(b, v) }

// ReportVersion is bumped whenever the report shape changes.
const ReportVersion = "1.0"

// Limitations states what a local hash chain cannot provide.
const Limitations = "The SHA-256 hash chain provides integrity and ordering " +
	"checks only. It does NOT provide trusted timestamps (any host can set its " +
	"own clock), non-repudiation, or proof of origin; those require external " +
	"trusted timestamping (RFC 3161) and/or digital signatures from a " +
	"recognized key, plus judicial/forensic accreditation procedures. This " +
	"system is not certified for legal-chain-of-custody compliance."

// Report is the full exported case record.
type Report struct {
	ReportVersion string                   `json:"report_version"`
	GeneratedAt   time.Time                `json:"generated_at"`
	Baseline      Baseline                 `json:"baseline"`
	Verifications []models.VerificationJob `json:"verifications"`
	Chain         ChainView                `json:"chain"`
	Limitations   string                   `json:"limitations"`
}

// Baseline captures the immutable registration record.
type Baseline struct {
	CaseRef      string    `json:"case_ref"`
	EvidenceFile string    `json:"evidence_file"`
	ResolvedPath string    `json:"resolved_path"`
	Size         int64     `json:"size"`
	SHA256       string    `json:"sha256"`
	MtimeNanos   int64     `json:"mtime_nanos"`
	Device       uint64    `json:"device"`
	Inode        uint64    `json:"inode"`
	RegisteredBy string    `json:"registered_by"`
	RegisteredAt time.Time `json:"registered_at"`
}

// EventView is one serialized chain entry.
type EventView struct {
	Seq           int       `json:"seq"`
	Type          string    `json:"type"`
	Actor         string    `json:"actor"`
	Payload       any       `json:"payload"`
	PrevDigest    string    `json:"prev_digest"`
	ContentDigest string    `json:"content_digest"`
	EntryDigest   string    `json:"entry_digest"`
	CreatedAt     time.Time `json:"created_at"`
}

// ChainView contains the ordered events plus their verification result.
type ChainView struct {
	OK         bool          `json:"ok"`
	LastDigest string        `json:"last_digest"`
	Faults     []chain.Fault `json:"faults,omitempty"`
	Events     []EventView   `json:"events"`
}

// Build loads everything needed for the export report.
func Build(db *gorm.DB, caseID uint) (*Report, error) {
	var c models.Case
	if err := db.First(&c, caseID).Error; err != nil {
		return nil, err
	}
	var jobs []models.VerificationJob
	if err := db.Where("case_id = ?", caseID).Order("id ASC").Find(&jobs).Error; err != nil {
		return nil, err
	}
	events, err := chain.List(db, caseID)
	if err != nil {
		return nil, err
	}
	verify, err := chain.Verify(db, caseID)
	if err != nil {
		return nil, err
	}
	views := make([]EventView, 0, len(events))
	for _, e := range events {
		var payload any
		if len(e.Payload) > 0 {
			_ = jsonUnmarshal(e.Payload, &payload)
		}
		views = append(views, EventView{
			Seq:           e.Seq,
			Type:          e.Type,
			Actor:         e.Actor,
			Payload:       payload,
			PrevDigest:    e.PrevDigest,
			ContentDigest: e.ContentDigest,
			EntryDigest:   e.EntryDigest,
			CreatedAt:     e.CreatedAt,
		})
	}
	return &Report{
		ReportVersion: ReportVersion,
		GeneratedAt:   time.Now().UTC(),
		Baseline: Baseline{
			CaseRef:      c.CaseRef,
			EvidenceFile: c.EvidenceFile,
			ResolvedPath: c.ResolvedPath,
			Size:         c.Size,
			SHA256:       c.SHA256,
			MtimeNanos:   c.MtimeNanos,
			Device:       c.Device,
			Inode:        c.Inode,
			RegisteredBy: c.RegisteredBy,
			RegisteredAt: c.RegisteredAt,
		},
		Verifications: jobs,
		Chain: ChainView{
			OK:         verify.OK,
			LastDigest: verify.LastDigest,
			Faults:     verify.Faults,
			Events:     views,
		},
		Limitations: Limitations,
	}, nil
}
