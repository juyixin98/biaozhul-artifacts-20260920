package integration

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

// assignResponder admin-assigns the responder so P1 triage gate passes.
func assignResponder(t *testing.T, h http.Handler, incidentID string) {
	t.Helper()
	body := `{"user_id":"` + uResponder + `","role":"responder"}`
	w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+incidentID+"/assignments",
		uAdmin, reqID(), body)
	if w.Code != http.StatusOK {
		t.Fatalf("assign responder: %d %s", w.Code, w.Body.String())
	}
}

// walkTo moves the incident through stages up to and including target using
// the correct role each time, returning the final version.
func walkTo(t *testing.T, h http.Handler, incidentID string, startVersion int64, target string) int64 {
	t.Helper()
	stageRole := map[string]string{
		"triaged": uAnalyst, "contained": uResponder, "eradicated": uResponder,
		"recovered": uResponder, "postmortem": uAnalyst, "closed": uResponder,
	}
	order := []string{"triaged", "contained", "eradicated", "recovered", "postmortem", "closed"}
	version := startVersion
	for _, st := range order {
		extra := ""
		if st == "postmortem" || st == "closed" {
			extra = `"root_cause":"phishing email","lessons_learned":"apply MFA"`
		}
		if st == "closed" {
			// Close gate requires >=1 action item with owner+due, none open.
			createAndCompleteActionItem(t, h, incidentID)
		}
		w := transitionAs(t, h, stageRole[st], incidentID, version, extra)
		if w.Code != http.StatusOK {
			t.Fatalf("walk transition to %s: %d %s", st, w.Code, w.Body.String())
		}
		version++
		if st == target {
			return version
		}
	}
	return version
}

// createAndCompleteActionItem makes one owned, due action item and closes it.
func createAndCompleteActionItem(t *testing.T, h http.Handler, incidentID string) {
	t.Helper()
	body := `{"title":"harden edge","owner_id":"` + uResponder + `","due_at":"2030-01-01T00:00:00Z"}`
	w := doJSON(t, h, http.MethodPost, "/v1/incidents/"+incidentID+"/action-items",
		uAnalyst, reqID(), body)
	if w.Code != http.StatusOK {
		t.Fatalf("create action item: %d %s", w.Code, w.Body.String())
	}
	var item struct {
		ID string `json:"id"`
	}
	decodeBody(t, w, &item)
	w = doJSON(t, h, http.MethodPost, "/v1/action-items/"+item.ID+"/status",
		uResponder, reqID(), `{"status":"done"}`)
	if w.Code != http.StatusOK {
		t.Fatalf("complete action item: %d %s", w.Code, w.Body.String())
	}
}

// TestFullLifecycle drives a P2 case all the way to closed with valid roles.
func TestFullLifecycle(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	svc := newService(t)
	h := newServer(t, svc)

	inc := apiCreateIncident(t, h, uAnalyst, "P2", "suspicious login")
	if inc.Stage != "detected" || inc.Version != 1 {
		t.Fatalf("unexpected initial state: %+v", inc)
	}
	assignResponder(t, h, inc.ID)

	version := walkTo(t, h, inc.ID, inc.Version, "closed")

	w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID, uAnalyst, "", "")
	if w.Code != http.StatusOK {
		t.Fatalf("get incident: %d", w.Code)
	}
	var got struct {
		Stage    string  `json:"stage"`
		Version  int64   `json:"version"`
		ClosedAt *string `json:"closed_at"`
	}
	decodeBody(t, w, &got)
	if got.Stage != "closed" || got.Version != version || got.ClosedAt == nil {
		t.Fatalf("final state wrong: %+v", got)
	}
}

// TestP1TriageGate: P1 cannot complete triage until a responder is assigned.
func TestP1TriageGate(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P1", "active breach")

	// Triage before assignment must be rejected with gate_failed.
	w := transitionAs(t, h, uAnalyst, inc.ID, 1, "")
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 gate failure, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "gate_failed")

	// Incident stays at detected, version unchanged.
	w = doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID, uAnalyst, "", "")
	var before struct {
		Stage   string `json:"stage"`
		Version int64  `json:"version"`
	}
	decodeBody(t, w, &before)
	if before.Stage != "detected" || before.Version != 1 {
		t.Fatalf("state changed despite gate rejection: %+v", before)
	}

	// After admin assigns responder, triage succeeds.
	assignResponder(t, h, inc.ID)
	w = transitionAs(t, h, uAnalyst, inc.ID, 1, "")
	if w.Code != http.StatusOK {
		t.Fatalf("triage after assignment: %d %s", w.Code, w.Body.String())
	}
}

// TestStaleVersionRejected: a transition carrying an old version is refused.
func TestStaleVersionRejected(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P3", "malware host")
	assignResponder(t, h, inc.ID)

	// First transition advances to triaged (version 1 -> 2).
	if w := transitionAs(t, h, uAnalyst, inc.ID, 1, ""); w.Code != http.StatusOK {
		t.Fatalf("first transition: %d %s", w.Code, w.Body.String())
	}
	// Replaying the same expected_version=1 must fail as a version conflict,
	// not silently re-advance.
	w := transitionAs(t, h, uResponder, inc.ID, 1, "")
	if w.Code != http.StatusConflict {
		t.Fatalf("expected 409 stale version, got %d %s", w.Code, w.Body.String())
	}
	assertCode(t, w, "version_conflict")

	// Current version 2 allows the responder to move onward.
	if w := transitionAs(t, h, uResponder, inc.ID, 2, ""); w.Code != http.StatusOK {
		t.Fatalf("current-version transition: %d %s", w.Code, w.Body.String())
	}
}

// TestCannotSkipStages: trying to jump from detected straight to, e.g., via
// repeated attempts — the API only ever offers the next edge; a responder
// cannot perform analyst-only triage and vice versa.
func TestCannotSkipStages(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P3", "dlp alert")
	assignResponder(t, h, inc.ID)

	// Responder tries to triage — analyst-only action -> forbidden, so
	// containment can never begin before triage completes.
	w := transitionAs(t, h, uResponder, inc.ID, 1, "")
	if w.Code != http.StatusForbidden {
		t.Fatalf("responder triage expected 403, got %d %s", w.Code, w.Body.String())
	}

	// After triage, analyst tries to contain — responder-only -> forbidden.
	if w := transitionAs(t, h, uAnalyst, inc.ID, 1, ""); w.Code != http.StatusOK {
		t.Fatalf("analyst triage: %d %s", w.Code, w.Body.String())
	}
	w = transitionAs(t, h, uAnalyst, inc.ID, 2, "")
	if w.Code != http.StatusForbidden {
		t.Fatalf("analyst containment expected 403, got %d %s", w.Code, w.Body.String())
	}
}

// TestTransitionRace: two concurrent transitions with the same expected
// version — exactly one wins, the other gets 409, and the version advances
// only once.
func TestTransitionRace(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P3", "race case")
	assignResponder(t, h, inc.ID)

	// Both concurrent calls use expected_version=1 (detected -> triaged),
	// but distinct request ids so idempotency doesn't coalesce them.
	type outcome struct {
		code int
		body string
	}
	out := make(chan outcome, 2)
	fire := func() {
		w := transitionAs(t, h, uAnalyst, inc.ID, 1, "")
		out <- outcome{w.Code, w.Body.String()}
	}
	go fire()
	fire()

	o1 := <-out
	o2 := <-out
	codes := []int{o1.code, o2.code}
	ok, conflict := 0, 0
	for _, c := range codes {
		switch c {
		case http.StatusOK:
			ok++
		case http.StatusConflict:
			conflict++
		default:
			t.Fatalf("unexpected race status %d", c)
		}
	}
	if ok != 1 || conflict != 1 {
		t.Fatalf("race: want 1 success + 1 conflict, got %d/%d (%s | %s)",
			ok, conflict, o1.body, o2.body)
	}

	w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID, uAnalyst, "", "")
	var got incidentBody
	decodeBody(t, w, &got)
	if got.Stage != "triaged" || got.Version != 2 {
		t.Fatalf("exactly one transition should apply: %+v", got)
	}

	// Exactly one stage_event row for version 2 (no half-updated state).
	var nEvents int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM stage_events WHERE incident_id=$1 AND version=2`,
		inc.ID).Scan(&nEvents); err != nil {
		t.Fatal(err)
	}
	if nEvents != 1 {
		t.Fatalf("want 1 stage event at v2, got %d", nEvents)
	}
}

// TestIdempotentReplay: same X-Request-Id returns the original result and does
// not advance state again.
func TestIdempotentReplay(t *testing.T) {
	truncateAll(t)
	seedUsers(t)
	h := newServer(t, newService(t))

	inc := apiCreateIncident(t, h, uAnalyst, "P3", "idem case")
	assignResponder(t, h, inc.ID)

	rid := reqID()
	first := transitionSameRequest(t, h, uAnalyst, inc.ID, rid, 1, "")
	if first.Code != http.StatusOK {
		t.Fatalf("first: %d %s", first.Code, first.Body.String())
	}
	second := transitionSameRequest(t, h, uAnalyst, inc.ID, rid, 1, "")
	if second.Code != http.StatusOK {
		t.Fatalf("replay: %d %s", second.Code, second.Body.String())
	}
	if second.Header().Get("Idempotent-Replay") != "true" {
		t.Fatalf("replay missing marker header: %+v", second.Header())
	}
	// Stored response round-trips through jsonb, which canonicalizes key
	// order; compare semantically rather than by raw bytes.
	var firstJSON, secondJSON any
	if err := json.Unmarshal(first.Body.Bytes(), &firstJSON); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(second.Body.Bytes(), &secondJSON); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(firstJSON, secondJSON) {
		t.Fatalf("replay returned different body:\n%s\n%s", first.Body, second.Body)
	}

	w := doJSON(t, h, http.MethodGet, "/v1/incidents/"+inc.ID, uAnalyst, "", "")
	var got incidentBody
	decodeBody(t, w, &got)
	if got.Stage != "triaged" || got.Version != 2 {
		t.Fatalf("replay advanced state again: %+v", got)
	}
}

func assertCode(t *testing.T, w *httptest.ResponseRecorder, want string) {
	t.Helper()
	var env struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	decodeBody(t, w, &env)
	if env.Error.Code != want {
		t.Fatalf("error code: want %q got %q (body=%s)", want, env.Error.Code, w.Body.String())
	}
}
