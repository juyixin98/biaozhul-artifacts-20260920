package httpapi

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"pilab/scheduler"
)

func TestHealth(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["status"] != "ok" {
		t.Fatalf("body = %v", body)
	}
}

func TestListAndRunScenario(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	// List contains the deadlock scenario.
	resp, err := http.Get(srv.URL + "/api/scenarios")
	if err != nil {
		t.Fatal(err)
	}
	var list []map[string]any
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if len(list) != 5 {
		t.Fatalf("want 5 scenarios, got %d", len(list))
	}

	// POST to run the AB-BA scenario.
	resp, err = http.Post(srv.URL+"/api/scenarios/deadlock-abba", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var rep scheduler.Report
	json.NewDecoder(resp.Body).Decode(&rep)
	resp.Body.Close()
	if !rep.Deadlocked {
		t.Error("expected deadlock report over HTTP")
	}
	sawCycle := false
	for _, e := range rep.Events {
		if e.Type == scheduler.EvDeadlock && len(e.Cycle) == 3 {
			sawCycle = true
		}
	}
	if !sawCycle {
		t.Error("deadlock cycle missing in streamed report")
	}
}

func TestSimulateCustomConfig(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	cfg := scheduler.Config{
		Locks: []scheduler.LockSpec{{ID: "A"}},
		Tasks: []scheduler.TaskSpec{
			{ID: "t", Base: 1, Arrival: 0, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 2},
			}},
		},
	}
	raw, _ := json.Marshal(cfg)
	resp, err := http.Post(srv.URL+"/api/simulate", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var rep scheduler.Report
	json.NewDecoder(resp.Body).Decode(&rep)
	resp.Body.Close()
	if !rep.Completed || rep.EndTick != 2 {
		t.Errorf("unexpected report completed=%v end=%d", rep.Completed, rep.EndTick)
	}
}

func TestSimulateInvalidConfig(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	// Unknown lock reference -> 422.
	bad := `{"locks":[{"id":"A"}],"tasks":[{"id":"t","basePriority":1,"arrival":0,"program":[{"op":"lock","lock":"ZZ"}]}]}`
	resp, err := http.Post(srv.URL+"/api/simulate", "application/json", strings.NewReader(bad))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, want 422", resp.StatusCode)
	}

	// Malformed JSON -> 400.
	resp2, err := http.Post(srv.URL+"/api/simulate", "application/json", strings.NewReader("{nope"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp2.StatusCode)
	}
}

func TestSSEStream(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	cfg := scheduler.Config{
		Locks: []scheduler.LockSpec{{ID: "A"}},
		Tasks: []scheduler.TaskSpec{
			{ID: "t", Base: 1, Arrival: 0, Program: scheduler.Program{
				{Op: scheduler.OpCPU, Ticks: 1},
			}},
		},
	}
	raw, _ := json.Marshal(cfg)
	resp, err := http.Post(srv.URL+"/api/simulate/stream", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "text/event-stream") {
		t.Fatalf("content-type = %q", ct)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	body := string(data)
	if !strings.Contains(body, "event: event") {
		t.Error("SSE stream missing per-event frames")
	}
	if !strings.Contains(body, "event: report") {
		t.Error("SSE stream missing final report frame")
	}
}

func TestCompareEndpoint(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/api/compare/inversion")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body map[string]json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if _, ok := body["highFinishTickSaved"]; !ok {
		t.Error("missing highFinishTickSaved metric")
	}
}

func TestMethodNotAllowed(t *testing.T) {
	srv := httptest.NewServer(NewServer().Handler())
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodDelete, srv.URL+"/healthz", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", resp.StatusCode)
	}
}
