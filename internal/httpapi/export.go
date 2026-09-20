package httpapi

import (
	"net/http"
	"time"
)

// exportDTO is the full incident report: identity, phase history, evidence
// summary, action items and duration metrics.
type exportDTO struct {
	Incident   incidentDTO        `json:"incident"`
	Phases     []phaseRecordDTO   `json:"phases"`
	Evidence   evidenceSummaryDTO `json:"evidence"`
	ActionItem []actionItemDTO    `json:"actionItems"`
	Metrics    metricsDTO         `json:"metrics"`
}

type phaseRecordDTO struct {
	Status    string    `json:"status"`
	EnteredAt time.Time `json:"enteredAt"`
	ActorID   *string   `json:"actorId"`
}

type evidenceSummaryDTO struct {
	Total int                  `json:"total"`
	Items []evidenceSummaryRow `json:"items"`
}

type evidenceSummaryRow struct {
	ID        string    `json:"id"`
	Seq       int32     `json:"seq"`
	AuthorID  string    `json:"authorId"`
	CreatedAt time.Time `json:"createdAt"`
	Excerpt   string    `json:"excerpt"`
	NoteCount int       `json:"noteCount"`
}

// metricsDTO holds durations derived only from recorded phase timestamps.
// A phase that never completed has no timestamp, so the corresponding
// duration stays null instead of being fabricated.
type metricsDTO struct {
	ContainmentDurationSeconds *float64 `json:"containmentDurationSeconds"`
	ResolutionDurationSeconds  *float64 `json:"resolutionDurationSeconds"`
}

func (s *Server) handleExport(w http.ResponseWriter, r *http.Request) {
	id, ae := urlUUID(r, "incidentID")
	if ae != nil {
		writeErr(w, ae)
		return
	}
	inc, err := s.q.GetIncident(r.Context(), pgUUID(id))
	if isNotFound(err) {
		writeErr(w, errNotFound)
		return
	}
	if err != nil {
		writeErr(w, err)
		return
	}
	transitions, err := s.q.ListTransitionsByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	evidence, err := s.q.ListEvidenceByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	notes, err := s.q.ListEvidenceNotesByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}
	items, err := s.q.ListActionItemsByIncident(r.Context(), pgUUID(id))
	if err != nil {
		writeErr(w, err)
		return
	}

	// Phase entry times come from the transition log; detection starts at
	// incident creation.
	phases := []phaseRecordDTO{{Status: "detected", EnteredAt: inc.CreatedAt.Time}}
	enteredAt := map[string]time.Time{"detected": inc.CreatedAt.Time}
	for _, t := range transitions {
		actor := fromPGUUID(t.ActorID).String()
		phases = append(phases, phaseRecordDTO{
			Status:    t.ToStatus,
			EnteredAt: t.CreatedAt.Time,
			ActorID:   &actor,
		})
		enteredAt[t.ToStatus] = t.CreatedAt.Time
	}

	noteCount := map[string]int{}
	for _, n := range notes {
		noteCount[fromPGUUID(n.EvidenceID).String()]++
	}
	summary := evidenceSummaryDTO{Total: len(evidence), Items: []evidenceSummaryRow{}}
	for _, e := range evidence {
		excerpt := e.Content
		if len(excerpt) > 200 {
			excerpt = excerpt[:200] + "…"
		}
		eid := fromPGUUID(e.ID).String()
		summary.Items = append(summary.Items, evidenceSummaryRow{
			ID:        eid,
			Seq:       e.Seq,
			AuthorID:  fromPGUUID(e.AuthorID).String(),
			CreatedAt: e.CreatedAt.Time,
			Excerpt:   excerpt,
			NoteCount: noteCount[eid],
		})
	}

	metrics := metricsDTO{}
	if containedAt, ok := enteredAt["contained"]; ok {
		d := containedAt.Sub(enteredAt["detected"]).Seconds()
		metrics.ContainmentDurationSeconds = &d
	}
	if closedAt, ok := enteredAt["closed"]; ok {
		d := closedAt.Sub(enteredAt["detected"]).Seconds()
		metrics.ResolutionDurationSeconds = &d
	}

	itemDTOs := make([]actionItemDTO, 0, len(items))
	for _, a := range items {
		itemDTOs = append(itemDTOs, toActionItemDTO(a))
	}

	writeJSON(w, http.StatusOK, exportDTO{
		Incident:   toIncidentDTO(inc),
		Phases:     phases,
		Evidence:   summary,
		ActionItem: itemDTOs,
		Metrics:    metrics,
	})
}
