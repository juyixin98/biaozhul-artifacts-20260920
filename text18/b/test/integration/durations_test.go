package integration

import (
	"net/http"
	"testing"
)

// TestDurationsUnfinishedAreNull: containment and resolution durations must
// be absent for a case that has not reached those stages — the service must
// not fabricate an end time (e.g. now()).
func TestDurationsUnfinishedAreNull(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "in progress")
	assignResponder(t, h, inc.ID)
	walkTo(t, h, inc.ID, 1, "triaged") // only triaged

	w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID+"/export",
		uAnalyst, "", "")
	if w.Code != http.StatusOK {
		t.Fatalf("export: %d %s", w.Code, w.Body.String())
	}
	var exp struct {
		Durations struct {
			ContainmentSeconds *float64 `json:"containment_seconds"`
			ResolutionSeconds  *float64 `json:"resolution_seconds"`
		} `json:"durations"`
		Incident struct {
			ContainedAt *string `json:"contained_at"`
			ClosedAt    *string `json:"closed_at"`
		} `json:"incident"`
	}
	decodeBody(t, w, &exp)
	if exp.Durations.ContainmentSeconds != nil {
		t.Fatalf("containment duration must be null while uncontained, got %v",
			*exp.Durations.ContainmentSeconds)
	}
	if exp.Durations.ResolutionSeconds != nil {
		t.Fatalf("resolution duration must be null while unclosed, got %v",
			*exp.Durations.ResolutionSeconds)
	}
	if exp.Incident.ContainedAt != nil || exp.Incident.ClosedAt != nil {
		t.Fatal("unreached stage timestamps must not be set")
	}
}

// TestDurationsClosedAreValid: a completed case yields non-negative durations,
// and resolution >= containment.
func TestDurationsClosedAreValid(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "completed")
	assignResponder(t, h, inc.ID)
	walkTo(t, h, inc.ID, 1, "closed")

	w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID+"/export",
		uAnalyst, "", "")
	if w.Code != http.StatusOK {
		t.Fatalf("export: %d %s", w.Code, w.Body.String())
	}
	var exp struct {
		Durations struct {
			ContainmentSeconds *float64 `json:"containment_seconds"`
			ResolutionSeconds  *float64 `json:"resolution_seconds"`
		} `json:"durations"`
		StageEvents []struct {
			ToStage string `json:"to_stage"`
			Version int64  `json:"version"`
		} `json:"stage_events"`
	}
	decodeBody(t, w, &exp)

	if exp.Durations.ContainmentSeconds == nil {
		t.Fatal("closed case must have a containment duration")
	}
	if exp.Durations.ResolutionSeconds == nil {
		t.Fatal("closed case must have a resolution duration")
	}
	if *exp.Durations.ContainmentSeconds < 0 || *exp.Durations.ResolutionSeconds < 0 {
		t.Fatal("durations must not be negative")
	}
	if *exp.Durations.ResolutionSeconds < *exp.Durations.ContainmentSeconds {
		t.Fatalf("resolution (%v) must be >= containment (%v)",
			*exp.Durations.ResolutionSeconds, *exp.Durations.ContainmentSeconds)
	}

	// Six transitions recorded, in order, no gaps.
	if len(exp.StageEvents) != 6 {
		t.Fatalf("want 6 stage events, got %d", len(exp.StageEvents))
	}
	want := []string{"triaged", "contained", "eradicated", "recovered", "postmortem", "closed"}
	for i, e := range exp.StageEvents {
		if e.ToStage != want[i] {
			t.Fatalf("stage event %d: want %s got %s", i, want[i], e.ToStage)
		}
		if e.Version != int64(i+2) {
			t.Fatalf("stage event %d: version want %d got %d", i, i+2, e.Version)
		}
	}
}

// TestExportContainsStageAndEvidenceSummary: export shape for a partially
// progressed case with evidence.
func TestExportContainsStageAndEvidenceSummary(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "summary")
	assignResponder(t, h, inc.ID)
	walkTo(t, h, inc.ID, 1, "contained") // version 3

	if w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+inc.ID+"/evidence",
		uAnalyst, reqID(), `{"content":"netflow capture"}`); w.Code != http.StatusOK {
		t.Fatalf("add evidence: %d %s", w.Code, w.Body.String())
	}

	w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID+"/export",
		uAnalyst, "", "")
	var exp struct {
		StageEvents []any `json:"stage_events"`
		Evidence    []struct {
			Content string `json:"content"`
		} `json:"evidence_summary"`
	}
	decodeBody(t, w, &exp)
	if len(exp.StageEvents) != 2 {
		t.Fatalf("want 2 stage events (triage, containment), got %d", len(exp.StageEvents))
	}
	if len(exp.Evidence) != 1 || exp.Evidence[0].Content != "netflow capture" {
		t.Fatalf("evidence summary wrong: %+v", exp.Evidence)
	}
}
