package service

import (
	"context"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"sircc/internal/domain"
	"sircc/internal/store"
)

// EvidenceSummaryItem is a compact evidence entry for exports: it never
// duplicates full body text beyond the configured preview length.
type EvidenceSummaryItem struct {
	Seq         int32  `json:"seq"`
	EvidenceID  string `json:"evidence_id"`
	SubmittedBy string `json:"submitted_by"`
	AuthorName  string `json:"author_full_name,omitempty"`
	CreatedAt   string `json:"created_at"`
	Length      int    `json:"content_length"`
	Preview     string `json:"preview"`
	NoteCount   int    `json:"note_count"`
}

type PhaseRecord struct {
	Phase       string   `json:"phase"`
	Actor       string   `json:"actor_username"`
	EnteredAt   string   `json:"entered_at"`
	DurationSec *float64 `json:"duration_seconds"`
}

// ExportReport is the case export: lifecycle phase records with durations,
// metrics derived from those records, and a summary of the evidence chain.
type ExportReport struct {
	IncidentID     string                `json:"incident_id"`
	Title          string                `json:"title"`
	Severity       string                `json:"severity"`
	Status         string                `json:"status"`
	Version        int64                 `json:"version"`
	DetectedAt     string                `json:"detected_at"`
	ClosedAt       *string               `json:"closed_at,omitempty"`
	RootCause      *string               `json:"root_cause,omitempty"`
	LessonsLearned *string               `json:"lessons_learned,omitempty"`
	Phases         []PhaseRecord         `json:"phases"`
	Metrics        *PhaseMetrics         `json:"metrics"`
	Evidence       []EvidenceSummaryItem `json:"evidence_summary"`
	ActionItems    []ActionItemView      `json:"action_items"`
	Reminders      []ReminderView        `json:"reminders"`
}

const exportPreviewRunes = 200

// Export builds the report from committed data only.
func (s *Service) Export(ctx context.Context, caller *store.User, incidentID uuid.UUID) (*ExportReport, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	q := store.New(tx)

	inc, err := q.GetIncident(ctx, incidentID)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, domain.ErrNotFound
		}
		return nil, err
	}
	if _, err := s.requireCaseAccess(ctx, q, incidentID, caller); err != nil {
		return nil, err
	}

	phaseRows, err := q.ListPhasesWithActor(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	times := phasesToTimes(phaseRows)
	report := &ExportReport{
		IncidentID:     inc.ID.String(),
		Title:          inc.Title,
		Severity:       inc.Severity,
		Status:         inc.Status,
		Version:        inc.Version,
		DetectedAt:     ts(inc.DetectedAt).Format("2006-01-02T15:04:05Z07:00"),
		RootCause:      inc.RootCause,
		LessonsLearned: inc.LessonsLearned,
		Metrics:        computeMetrics(times),
	}
	if inc.ClosedAt.Valid {
		c := ts(inc.ClosedAt).Format("2006-01-02T15:04:05Z07:00")
		report.ClosedAt = &c
	}

	// Phase records with each phase's duration (null for the open tail).
	report.Phases = make([]PhaseRecord, 0, len(phaseRows))
	phaseOrder := map[string]string{
		"detection":   "triage",
		"triage":      "containment",
		"containment": "eradication",
		"eradication": "recovery",
		"recovery":    "review",
		"review":      "closure",
	}
	for _, p := range phaseRows {
		var dur *float64
		if next := phaseOrder[p.Phase]; next != "" {
			dur = secondsBetween(times[p.Phase], times[next])
		}
		report.Phases = append(report.Phases, PhaseRecord{
			Phase:       p.Phase,
			Actor:       p.ActorUsername,
			EnteredAt:   ts(p.EnteredAt).Format("2006-01-02T15:04:05Z07:00"),
			DurationSec: dur,
		})
	}

	evRows, err := q.ListEvidenceWithAuthor(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	notes, err := q.ListNotesByIncidentWithAuthor(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	noteCount := map[uuid.UUID]int{}
	for _, n := range notes {
		noteCount[n.EvidenceID]++
	}
	report.Evidence = make([]EvidenceSummaryItem, 0, len(evRows))
	for _, e := range evRows {
		preview := e.Content
		if runes := []rune(preview); len(runes) > exportPreviewRunes {
			preview = string(runes[:exportPreviewRunes]) + "…"
		}
		report.Evidence = append(report.Evidence, EvidenceSummaryItem{
			Seq:         e.Seq,
			EvidenceID:  e.ID.String(),
			SubmittedBy: e.SubmittedBy.String(),
			AuthorName:  e.AuthorFullName,
			CreatedAt:   ts(e.CreatedAt).Format("2006-01-02T15:04:05Z07:00"),
			Length:      len(e.Content),
			Preview:     preview,
			NoteCount:   noteCount[e.ID],
		})
	}

	items, err := q.ListActionItemsByIncident(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	report.ActionItems = make([]ActionItemView, 0, len(items))
	for _, it := range items {
		report.ActionItems = append(report.ActionItems, actionItemRowView(it))
	}
	remRows, err := q.ListRemindersByIncident(ctx, incidentID)
	if err != nil {
		return nil, err
	}
	report.Reminders = make([]ReminderView, 0, len(remRows))
	for _, r := range remRows {
		report.Reminders = append(report.Reminders, reminderView(r))
	}
	return report, tx.Commit(ctx)
}
