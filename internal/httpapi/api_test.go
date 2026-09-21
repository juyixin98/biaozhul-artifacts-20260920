// Integration tests for the SIRCC API. They require a PostgreSQL database:
//
//	DATABASE_URL=postgres://sircc:postgres@localhost:5432/sircc?sslmode=disable go test ./...
//
// Tests skip silently when DATABASE_URL is unset.
package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"sircc/internal/httpapi"
	"sircc/internal/incident"
	"sircc/internal/migrations"
	"sircc/internal/reminder"
)

type env struct {
	base string
	pool *pgxpool.Pool
}

func setup(t *testing.T) *env {
	t.Helper()
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		t.Skip("DATABASE_URL not set; skipping integration test")
	}
	if err := migrations.Up(dsn); err != nil {
		t.Fatalf("migrations: %v", err)
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatalf("parse dsn: %v", err)
	}
	cfg.MaxConns = 16
	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	if _, err := pool.Exec(context.Background(), "TRUNCATE incidents CASCADE"); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	srv := httptest.NewServer(httpapi.NewRouter(incident.NewService(pool)))
	t.Cleanup(func() {
		srv.Close()
		pool.Close()
	})
	return &env{base: srv.URL, pool: pool}
}

// call performs an authenticated JSON request and returns status + body.
func (e *env) call(t *testing.T, method, path, user, role string, body any) (int, []byte) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, e.base+path, rdr)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	if user != "" {
		req.Header.Set("X-User-ID", user)
	}
	if role != "" {
		req.Header.Set("X-User-Role", role)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data
}

func decode(t *testing.T, data []byte, v any) {
	t.Helper()
	if err := json.Unmarshal(data, v); err != nil {
		t.Fatalf("decode %s: %v", data, err)
	}
}

func errCode(t *testing.T, data []byte) string {
	t.Helper()
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	decode(t, data, &body)
	return body.Error.Code
}

func (e *env) createIncident(t *testing.T, severity string) (id string, version int32) {
	t.Helper()
	st, body := e.call(t, "POST", "/incidents", "admin1", "admin",
		map[string]string{"title": "test incident", "severity": severity})
	if st != http.StatusCreated {
		t.Fatalf("create incident: %d %s", st, body)
	}
	var inc struct {
		ID      string `json:"id"`
		Version int32  `json:"version"`
	}
	decode(t, body, &inc)
	return inc.ID, inc.Version
}

func (e *env) assign(t *testing.T, id, user, role string) {
	t.Helper()
	st, body := e.call(t, "POST", "/incidents/"+id+"/members", "admin1", "admin",
		map[string]string{"user_id": user, "role": role})
	if st != http.StatusCreated {
		t.Fatalf("assign %s/%s: %d %s", user, role, st, body)
	}
}

func (e *env) transition(t *testing.T, id, to string, version int32, reqID, user, role string) (int, []byte) {
	t.Helper()
	return e.call(t, "POST", "/incidents/"+id+"/transitions", user, role,
		map[string]any{"to": to, "expected_version": version, "request_id": reqID})
}

// mustTransition asserts a transition succeeds and returns the new version.
func (e *env) mustTransition(t *testing.T, id, to string, version int32, reqID, user, role string) int32 {
	t.Helper()
	st, body := e.transition(t, id, to, version, reqID, user, role)
	if st != http.StatusOK {
		t.Fatalf("transition to %s: %d %s", to, st, body)
	}
	var res struct {
		Incident struct {
			Version int32 `json:"version"`
		} `json:"incident"`
	}
	decode(t, body, &res)
	return res.Incident.Version
}

// driveToPostmortem walks an incident from detected to postmortem and returns
// the current version.
func (e *env) driveToPostmortem(t *testing.T, id string, version int32) int32 {
	t.Helper()
	e.assign(t, id, "ana1", "analyst")
	e.assign(t, id, "res1", "responder")
	steps := []struct{ to, user, role string }{
		{"triaged", "ana1", "analyst"},
		{"contained", "res1", "responder"},
		{"eradicated", "res1", "responder"},
		{"recovered", "res1", "responder"},
		{"postmortem", "res1", "responder"},
	}
	for i, s := range steps {
		version = e.mustTransition(t, id, s.to, version, fmt.Sprintf("walk-%d", i), s.user, s.role)
	}
	return version
}

// --- tests --------------------------------------------------------------------

func TestTransitionRaceExactlyOneWins(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P3")
	e.assign(t, id, "ana1", "analyst")

	const racers = 8
	var wg sync.WaitGroup
	results := make(chan int, racers)
	for i := 0; i < racers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			st, _ := e.transition(t, id, "triaged", version, fmt.Sprintf("race-%d", i), "ana1", "analyst")
			results <- st
		}(i)
	}
	wg.Wait()
	close(results)

	wins, conflicts := 0, 0
	for st := range results {
		switch st {
		case http.StatusOK:
			wins++
		case http.StatusConflict:
			conflicts++
		default:
			t.Fatalf("unexpected status %d", st)
		}
	}
	if wins != 1 || conflicts != racers-1 {
		t.Fatalf("want 1 win and %d conflicts, got %d wins, %d conflicts", racers-1, wins, conflicts)
	}
}

func TestIdempotentReplayAndStaleVersion(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P3")
	e.assign(t, id, "ana1", "analyst")

	st, body := e.transition(t, id, "triaged", version, "req-1", "ana1", "analyst")
	if st != http.StatusOK {
		t.Fatalf("first transition: %d %s", st, body)
	}
	// Same request_id returns the original result without re-applying.
	st, body = e.transition(t, id, "triaged", version, "req-1", "ana1", "analyst")
	if st != http.StatusOK {
		t.Fatalf("replay: %d %s", st, body)
	}
	var replay struct {
		Replayed bool `json:"replayed"`
	}
	decode(t, body, &replay)
	if !replay.Replayed {
		t.Fatalf("expected replayed=true, got %s", body)
	}
	st, body = e.call(t, "GET", "/incidents/"+id, "ana1", "analyst", nil)
	var view struct {
		Incident struct {
			Version int32  `json:"version"`
			Status  string `json:"status"`
		} `json:"incident"`
	}
	decode(t, body, &view)
	if view.Incident.Version != 2 || view.Incident.Status != "triaged" {
		t.Fatalf("replay mutated state: %+v", view.Incident)
	}

	// Stale expected_version is rejected.
	st, body = e.transition(t, id, "contained", 1, "req-2", "ana1", "analyst")
	if st != http.StatusConflict || errCode(t, body) != "VERSION_CONFLICT" {
		t.Fatalf("stale version: %d %s", st, body)
	}

	// Same request_id on a different incident is a conflict.
	id2, _ := e.createIncident(t, "P4")
	st, body = e.transition(t, id2, "triaged", 1, "req-1", "ana1", "analyst")
	if st != http.StatusConflict || errCode(t, body) != "REQUEST_ID_CONFLICT" {
		t.Fatalf("request id reuse: %d %s", st, body)
	}
}

func TestNoPhaseSkipping(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P2")
	e.assign(t, id, "res1", "responder")

	st, body := e.transition(t, id, "contained", version, "skip-1", "res1", "responder")
	if st != http.StatusBadRequest || errCode(t, body) != "INVALID_TRANSITION" {
		t.Fatalf("skip detected->contained: %d %s", st, body)
	}
	st, body = e.transition(t, id, "closed", version, "skip-2", "res1", "responder")
	if st != http.StatusBadRequest || errCode(t, body) != "INVALID_TRANSITION" {
		t.Fatalf("skip detected->closed: %d %s", st, body)
	}
}

func TestP1TriageRequiresResponder(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P1")
	e.assign(t, id, "ana1", "analyst")

	st, body := e.transition(t, id, "triaged", version, "p1-1", "ana1", "analyst")
	if st != http.StatusBadRequest || errCode(t, body) != "GATE_UNMET" {
		t.Fatalf("P1 triage without responder: %d %s", st, body)
	}

	e.assign(t, id, "res1", "responder")
	e.mustTransition(t, id, "triaged", version, "p1-2", "ana1", "analyst")
}

func TestCloseGates(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P2")
	version = e.driveToPostmortem(t, id, version)

	// No root cause / lessons / action items yet.
	st, body := e.transition(t, id, "closed", version, "close-1", "res1", "responder")
	if st != http.StatusBadRequest || errCode(t, body) != "GATE_UNMET" {
		t.Fatalf("close without postmortem: %d %s", st, body)
	}

	// Record postmortem (responder only, postmortem phase only).
	st, body = e.call(t, "PUT", "/incidents/"+id+"/postmortem", "res1", "responder",
		map[string]string{"root_cause": "stolen credentials", "lessons_learned": "enforce MFA"})
	if st != http.StatusOK {
		t.Fatalf("set postmortem: %d %s", st, body)
	}
	version++ // postmortem update bumps the version

	// Still blocked: no action items.
	st, body = e.transition(t, id, "closed", version, "close-2", "res1", "responder")
	if st != http.StatusBadRequest || errCode(t, body) != "GATE_UNMET" {
		t.Fatalf("close without action items: %d %s", st, body)
	}

	st, body = e.call(t, "POST", "/incidents/"+id+"/action-items", "res1", "responder",
		map[string]string{
			"title": "roll out MFA", "owner_id": "res1",
			"due_at": time.Now().Add(72 * time.Hour).UTC().Format(time.RFC3339),
		})
	if st != http.StatusCreated {
		t.Fatalf("create action item: %d %s", st, body)
	}

	e.mustTransition(t, id, "closed", version, "close-3", "res1", "responder")
}

func TestEvidenceCapacityAndConcurrency(t *testing.T) {
	e := setup(t)
	id, _ := e.createIncident(t, "P3")
	e.assign(t, id, "ana1", "analyst")

	// Sequential fill to the cap.
	for i := 0; i < incident.MaxEvidence; i++ {
		st, body := e.call(t, "POST", "/incidents/"+id+"/evidence", "ana1", "analyst",
			map[string]string{"content": fmt.Sprintf("log line %d", i)})
		if st != http.StatusCreated {
			t.Fatalf("evidence %d: %d %s", i, st, body)
		}
	}
	st, body := e.call(t, "POST", "/incidents/"+id+"/evidence", "ana1", "analyst",
		map[string]string{"content": "one too many"})
	if st != http.StatusConflict || errCode(t, body) != "EVIDENCE_LIMIT" {
		t.Fatalf("51st evidence: %d %s", st, body)
	}

	// Concurrent adds cannot bypass the cap.
	id2, _ := e.createIncident(t, "P3")
	e.assign(t, id2, "ana1", "analyst")
	const attempts = 60
	var wg sync.WaitGroup
	codes := make(chan int, attempts)
	for i := 0; i < attempts; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			st, _ := e.call(t, "POST", "/incidents/"+id2+"/evidence", "ana1", "analyst",
				map[string]string{"content": fmt.Sprintf("concurrent %d", i)})
			codes <- st
		}(i)
	}
	wg.Wait()
	close(codes)
	created := 0
	for st := range codes {
		if st == http.StatusCreated {
			created++
		}
	}
	if created != incident.MaxEvidence {
		t.Fatalf("concurrent adds created %d evidence entries, want %d", created, incident.MaxEvidence)
	}
}

func TestEvidenceImmutableCorrectionViaNotes(t *testing.T) {
	e := setup(t)
	id, _ := e.createIncident(t, "P3")
	e.assign(t, id, "ana1", "analyst")

	st, body := e.call(t, "POST", "/incidents/"+id+"/evidence", "ana1", "analyst",
		map[string]string{"content": "original finding"})
	if st != http.StatusCreated {
		t.Fatalf("add evidence: %d %s", st, body)
	}
	var ev struct {
		ID string `json:"id"`
	}
	decode(t, body, &ev)

	// No endpoint allows overwriting evidence.
	st, _ = e.call(t, "PUT", "/incidents/"+id+"/evidence", "ana1", "analyst",
		map[string]string{"content": "tampered"})
	if st != http.StatusMethodNotAllowed {
		t.Fatalf("overwrite attempt: got %d, want 405", st)
	}

	// Correction is an appended note.
	st, body = e.call(t, "POST", "/evidence/"+ev.ID+"/notes", "ana1", "analyst",
		map[string]string{"note": "correction: timestamp was UTC+2"})
	if st != http.StatusCreated {
		t.Fatalf("add note: %d %s", st, body)
	}

	// Export shows the original content plus the correction note.
	st, body = e.call(t, "GET", "/incidents/"+id+"/export", "ana1", "analyst", nil)
	if st != http.StatusOK {
		t.Fatalf("export: %d %s", st, body)
	}
	var exp struct {
		Evidence struct {
			Count int `json:"count"`
			Items []struct {
				Excerpt string `json:"excerpt"`
				Notes   []struct {
					Note string `json:"note"`
				} `json:"notes"`
			} `json:"items"`
		} `json:"evidence"`
	}
	decode(t, body, &exp)
	if exp.Evidence.Count != 1 || exp.Evidence.Items[0].Excerpt != "original finding" {
		t.Fatalf("evidence was modified: %s", body)
	}
	if len(exp.Evidence.Items[0].Notes) != 1 {
		t.Fatalf("correction note missing: %s", body)
	}
}

func TestReminderRescheduleAndDeduplication(t *testing.T) {
	e := setup(t)
	id, _ := e.createIncident(t, "P3")
	e.assign(t, id, "res1", "responder")
	worker := &reminder.Worker{Pool: e.pool}
	ctx := context.Background()

	past := time.Now().Add(-time.Hour).UTC().Format(time.RFC3339)
	st, body := e.call(t, "POST", "/incidents/"+id+"/action-items", "res1", "responder",
		map[string]string{"title": "patch hosts", "owner_id": "res1", "due_at": past})
	if st != http.StatusCreated {
		t.Fatalf("create action item: %d %s", st, body)
	}
	var item struct {
		ID string `json:"id"`
	}
	decode(t, body, &item)

	countReminders := func() int {
		st, body := e.call(t, "GET", "/incidents/"+id+"/reminders", "res1", "responder", nil)
		if st != http.StatusOK {
			t.Fatalf("list reminders: %d %s", st, body)
		}
		var out struct {
			Reminders []any `json:"reminders"`
		}
		decode(t, body, &out)
		return len(out.Reminders)
	}

	// Due now: exactly one persistent reminder, and re-running (as after a
	// restart) does not duplicate it.
	if n, err := worker.RunOnce(ctx); err != nil || n != 1 {
		t.Fatalf("first pass: sent=%d err=%v", n, err)
	}
	if n, err := worker.RunOnce(ctx); err != nil || n != 0 {
		t.Fatalf("second pass (restart catch-up): sent=%d err=%v", n, err)
	}
	if got := countReminders(); got != 1 {
		t.Fatalf("reminders after dedup pass: %d, want 1", got)
	}

	// Reschedule into the future: the old schedule must not fire again.
	future := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	st, body = e.call(t, "POST", "/action-items/"+item.ID+"/reschedule", "res1", "responder",
		map[string]string{"due_at": future})
	if st != http.StatusOK {
		t.Fatalf("reschedule to future: %d %s", st, body)
	}
	if n, err := worker.RunOnce(ctx); err != nil || n != 0 {
		t.Fatalf("pass after future reschedule: sent=%d err=%v", n, err)
	}
	if got := countReminders(); got != 1 {
		t.Fatalf("stale schedule fired: %d reminders, want 1", got)
	}

	// Reschedule back into the past: the new due_version fires once more.
	st, body = e.call(t, "POST", "/action-items/"+item.ID+"/reschedule", "res1", "responder",
		map[string]string{"due_at": past})
	if st != http.StatusOK {
		t.Fatalf("reschedule to past: %d %s", st, body)
	}
	if n, err := worker.RunOnce(ctx); err != nil || n != 1 {
		t.Fatalf("pass for new due version: sent=%d err=%v", n, err)
	}
	if n, err := worker.RunOnce(ctx); err != nil || n != 0 {
		t.Fatalf("duplicate scheduling: sent=%d err=%v", n, err)
	}
	if got := countReminders(); got != 2 {
		t.Fatalf("reminders after reschedule cycle: %d, want 2", got)
	}
}

func TestUnauthorizedAndCaseScope(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P3")
	e.assign(t, id, "ana1", "analyst")

	// Missing/invalid auth headers.
	if st, _ := e.call(t, "GET", "/incidents/"+id, "", "", nil); st != http.StatusUnauthorized {
		t.Fatalf("no headers: %d, want 401", st)
	}
	if st, _ := e.call(t, "GET", "/incidents/"+id, "x", "superuser", nil); st != http.StatusUnauthorized {
		t.Fatalf("bad role: %d, want 401", st)
	}

	// Responder cannot triage (analyst-only operation).
	st, body := e.transition(t, id, "triaged", version, "unauth-1", "res1", "responder")
	if st != http.StatusForbidden {
		t.Fatalf("responder triage: %d %s, want 403", st, body)
	}

	// Analyst not assigned to the case cannot add evidence (case scope).
	st, body = e.call(t, "POST", "/incidents/"+id+"/evidence", "outsider", "analyst",
		map[string]string{"content": "nope"})
	if st != http.StatusForbidden {
		t.Fatalf("outsider evidence: %d %s, want 403", st, body)
	}

	// Non-admin cannot assign personnel.
	st, body = e.call(t, "POST", "/incidents/"+id+"/members", "ana1", "analyst",
		map[string]string{"user_id": "res9", "role": "responder"})
	if st != http.StatusForbidden {
		t.Fatalf("analyst assign: %d %s, want 403", st, body)
	}

	// Responder cannot push triage forward either.
	st, body = e.call(t, "POST", "/incidents/"+id+"/action-items", "res1", "responder",
		map[string]string{"title": "x", "owner_id": "res1",
			"due_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)})
	if st != http.StatusForbidden {
		t.Fatalf("unassigned responder action item: %d %s, want 403", st, body)
	}
}

func TestDurationsOnlyFromValidPhases(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P3")
	e.assign(t, id, "ana1", "analyst")
	e.assign(t, id, "res1", "responder")

	get := func() (contain, resolve *float64) {
		st, body := e.call(t, "GET", "/incidents/"+id, "ana1", "analyst", nil)
		if st != http.StatusOK {
			t.Fatalf("get incident: %d %s", st, body)
		}
		var view struct {
			Durations struct {
				Contain *float64 `json:"time_to_contain_seconds"`
				Resolve *float64 `json:"time_to_resolve_seconds"`
			} `json:"durations"`
		}
		decode(t, body, &view)
		return view.Durations.Contain, view.Durations.Resolve
	}

	// Only the detected phase exists: no durations at all.
	c, r := get()
	if c != nil || r != nil {
		t.Fatalf("fresh incident has fabricated durations: contain=%v resolve=%v", c, r)
	}

	version = e.mustTransition(t, id, "triaged", version, "dur-1", "ana1", "analyst")
	version = e.mustTransition(t, id, "contained", version, "dur-2", "res1", "responder")
	c, r = get()
	if c == nil || *c < 0 {
		t.Fatalf("containment duration missing after contained phase: %v", c)
	}
	if r != nil {
		t.Fatalf("resolution duration fabricated before recovery: %v", *r)
	}
}

func TestExportContainsPhasesAndEvidence(t *testing.T) {
	e := setup(t)
	id, version := e.createIncident(t, "P2")
	version = e.driveToPostmortem(t, id, version)

	st, body := e.call(t, "POST", "/incidents/"+id+"/evidence", "ana1", "analyst",
		map[string]string{"content": "pcap summary"})
	if st != http.StatusCreated {
		t.Fatalf("evidence: %d %s", st, body)
	}

	st, body = e.call(t, "GET", "/incidents/"+id+"/export", "res1", "responder", nil)
	if st != http.StatusOK {
		t.Fatalf("export: %d %s", st, body)
	}
	var exp struct {
		Phases []struct {
			Phase    string  `json:"phase"`
			ExitedAt *string `json:"exited_at"`
		} `json:"phases"`
		Evidence struct {
			Count int `json:"count"`
		} `json:"evidence"`
		AuditEvents []struct {
			Action string `json:"action"`
		} `json:"audit_events"`
	}
	decode(t, body, &exp)
	// detected..postmortem = 6 phase records; all but the current one exited.
	if len(exp.Phases) != 6 {
		t.Fatalf("phases: %d, want 6", len(exp.Phases))
	}
	for i, p := range exp.Phases {
		if i < len(exp.Phases)-1 && p.ExitedAt == nil {
			t.Fatalf("phase %d (%s) missing exit time", i, p.Phase)
		}
	}
	if exp.Phases[len(exp.Phases)-1].ExitedAt != nil {
		t.Fatalf("current phase has a fabricated exit time")
	}
	if exp.Evidence.Count != 1 {
		t.Fatalf("evidence count: %d, want 1", exp.Evidence.Count)
	}
	if len(exp.AuditEvents) == 0 {
		t.Fatalf("no audit events in export")
	}
}
