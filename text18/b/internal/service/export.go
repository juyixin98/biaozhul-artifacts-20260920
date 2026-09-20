package service

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"

	"sircc/internal/db"
	"sircc/internal/domain"
)

// Duration metrics are computed ONLY from stages that were actually reached
// through a valid transition. An un-reached endpoint leaves the duration nil;
// we never synthesize an end timestamp (e.g. now()) for unfinished work.

type durations struct {
	// Containment: triaged_at -> contained_at.
	ContainmentSeconds *float64 `json:"containment_seconds,omitempty"`
	// Resolution: triaged_at -> closed_at.
	ResolutionSeconds *float64 `json:"resolution_seconds,omitempty"`
}

func secondsBetween(a, b *time.Time) *float64 {
	if a == nil || b == nil {
		return nil
	}
	d := b.Sub(*a).Seconds()
	return &d
}

func computeDurations(inc db.Incident) durations {
	triaged := ts(inc.TriagedAt)
	contained := ts(inc.ContainedAt)
	closed := ts(inc.ClosedAt)
	return durations{
		ContainmentSeconds: secondsBetween(triaged, contained),
		ResolutionSeconds:  secondsBetween(triaged, closed),
	}
}

type exportEvidence struct {
	ID          string       `json:"id"`
	Content     string       `json:"content"`
	SubmittedBy string       `json:"submitted_by"`
	CreatedAt   time.Time    `json:"created_at"`
	Notes       []exportNote `json:"notes"`
}

type exportNote struct {
	ID        string    `json:"id"`
	Content   string    `json:"content"`
	AddedBy   string    `json:"added_by"`
	CreatedAt time.Time `json:"created_at"`
}

type exportPayload struct {
	Incident    incidentResponse `json:"incident"`
	Durations   durations        `json:"durations"`
	StageEvents []db.StageEvent  `json:"stage_events"`
	Evidence    []exportEvidence `json:"evidence_summary"`
	ActionItems []db.ActionItem  `json:"action_items"`
	ExportedAt  time.Time        `json:"exported_at"`
}

// ExportIncident returns the full case record: stage timeline, evidence with
// appended corrections, action items, and valid-only duration metrics.
func (s *Service) ExportIncident(w http.ResponseWriter, r *http.Request) {
	incidentID := chi.URLParam(r, "incidentID")
	actor, _ := actorFrom(r.Context())
	if !s.canRead(w, r, actor, incidentID) {
		return
	}

	inc, err := s.q.GetIncident(r.Context(), incidentID)
	if err != nil {
		return // canRead already responded
	}
	events, err := s.q.ListStageEvents(r.Context(), incidentID)
	if err != nil {
		http500(w, "stage events")
		return
	}
	items, err := s.q.ListActionItems(r.Context(), incidentID)
	if err != nil {
		http500(w, "action items")
		return
	}

	// Join evidence with correction notes, grouping rows into evidence records.
	joined, err := s.q.ListEvidenceWithNotes(r.Context(), incidentID)
	if err != nil {
		http500(w, "evidence")
		return
	}
	evidence := make([]exportEvidence, 0)
	idxByEvidence := map[string]int{}
	for _, row := range joined {
		idx, ok := idxByEvidence[row.EvidenceID]
		if !ok {
			idx = len(evidence)
			idxByEvidence[row.EvidenceID] = idx
			evidence = append(evidence, exportEvidence{
				ID:          row.EvidenceID,
				Content:     row.EvidenceContent,
				SubmittedBy: row.EvidenceSubmittedBy,
				CreatedAt:   row.EvidenceCreatedAt.Time,
				Notes:       []exportNote{},
			})
		}
		if row.NoteID.Valid {
			evidence[idx].Notes = append(evidence[idx].Notes, exportNote{
				ID:        row.NoteID.String,
				Content:   row.NoteContent.String,
				AddedBy:   row.NoteAddedBy.String,
				CreatedAt: row.NoteCreatedAt.Time,
			})
		}
	}

	payload := exportPayload{
		Incident:    toIncidentResponse(inc),
		Durations:   computeDurations(inc),
		StageEvents: events,
		Evidence:    evidence,
		ActionItems: items,
		ExportedAt:  time.Now().UTC(),
	}

	// ?format=json (default) or download as an attachment.
	w.Header().Set("Content-Type", "application/json")
	if r.URL.Query().Get("download") == "1" {
		w.Header().Set("Content-Disposition", `attachment; filename="incident-`+incidentID+`.json"`)
	}
	writeOK(w, payload)
}

// StageSummary is a small read-only helper used by docs/demo.
func ValidStages() []string { return domain.OrderedStages }
