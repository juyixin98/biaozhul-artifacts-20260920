package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"deadline-admission/scheduler"
)

func newTestServer(t *testing.T) (*httptest.Server, *scheduler.Scheduler) {
	t.Helper()
	clock := scheduler.RealClock{}
	sch := scheduler.New(clock, scheduler.SleepExecutor{Clock: clock}, scheduler.NewEventLog(clock))
	sch.Start()
	t.Cleanup(sch.Stop)
	return httptest.NewServer(NewServer(sch)), sch
}

func post(t *testing.T, url string, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(url, "application/json", bytes.NewBufferString(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func getJSON(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func TestSubmitCompleteAndStats(t *testing.T) {
	ts, _ := newTestServer(t)
	defer ts.Close()

	code, body := post(t, ts.URL+"/jobs",
		`{"id":"quick","exec_bound_ms":200,"deadline_in_ms":30000,"simulate_actual_ms":20}`)
	if code != http.StatusCreated {
		t.Fatalf("submit = %d %v, want 201", code, body)
	}

	var job map[string]any
	for i := 0; i < 500; i++ {
		code, body = getJSON(t, ts.URL+"/jobs/quick")
		if code != 200 {
			t.Fatalf("get = %d, want 200", code)
		}
		job = body["job"].(map[string]any)
		if job["state"] == "COMPLETED" {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if job["state"] != "COMPLETED" {
		t.Fatalf("job state = %v, want COMPLETED", job["state"])
	}
	if job["deadline_met"] != true {
		t.Fatalf("deadline_met = %v, want true", job["deadline_met"])
	}

	code, stats := getJSON(t, ts.URL+"/stats")
	if code != 200 {
		t.Fatalf("stats = %d", code)
	}
	if stats["completed"] != float64(1) || stats["deadline_met"] != float64(1) {
		t.Fatalf("stats = %v", stats)
	}
	if stats["slot_acquired"] != stats["slot_released"] {
		t.Fatalf("slot not conserved: %v", stats)
	}
}

func TestSubmitInfeasibleReturns422(t *testing.T) {
	ts, _ := newTestServer(t)
	defer ts.Close()

	// Occupy the machine: bound 2s, actually runs 2s.
	code, _ := post(t, ts.URL+"/jobs",
		`{"id":"long","exec_bound_ms":2000,"deadline_in_ms":60000,"simulate_actual_ms":2000}`)
	if code != http.StatusCreated {
		t.Fatalf("submit long = %d, want 201", code)
	}
	// A job whose own bound already exceeds its deadline allowance is
	// predicted infeasible: earliest finish ~= now + 2000 (running) + 1500
	// > now + 500.
	code, body := post(t, ts.URL+"/jobs",
		`{"id":"hopeless","exec_bound_ms":1500,"deadline_in_ms":500}`)
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("submit hopeless = %d %v, want 422", code, body)
	}
	if body["error"] != "infeasible" {
		t.Fatalf("body = %v, want error=infeasible", body)
	}
}

func TestCancelQueuedJob(t *testing.T) {
	ts, _ := newTestServer(t)
	defer ts.Close()

	post(t, ts.URL+"/jobs",
		`{"id":"first","exec_bound_ms":1500,"deadline_in_ms":60000,"simulate_actual_ms":1500}`)
	code, _ := post(t, ts.URL+"/jobs",
		`{"id":"second","exec_bound_ms":100,"deadline_in_ms":60000}`)
	if code != http.StatusCreated {
		t.Fatalf("submit second = %d, want 201", code)
	}

	code, body := post(t, ts.URL+"/jobs/second/cancel", `{}`)
	if code != http.StatusOK {
		t.Fatalf("cancel = %d %v, want 200", code, body)
	}
	if body["job"].(map[string]any)["state"] != "CANCELLED" {
		t.Fatalf("state = %v, want CANCELLED", body)
	}
	// Duplicate cancel must be a conflict, not a second release.
	code, _ = post(t, ts.URL+"/jobs/second/cancel", `{}`)
	if code != http.StatusConflict {
		t.Fatalf("duplicate cancel = %d, want 409", code)
	}

	code, _ = post(t, ts.URL+"/jobs/no-such/cancel", `{}`)
	if code != http.StatusNotFound {
		t.Fatalf("cancel missing = %d, want 404", code)
	}

	// Let the first job finish, then check conservation via events.
	time.Sleep(2 * time.Second)
	code, stats := getJSON(t, ts.URL+"/stats")
	if stats["slot_acquired"] != stats["slot_released"] || stats["slot_in_use"] != false {
		t.Fatalf("slot not conserved: %v (code %d)", stats, code)
	}
}

func TestEventsEndpoint(t *testing.T) {
	ts, _ := newTestServer(t)
	defer ts.Close()

	post(t, ts.URL+"/jobs",
		`{"id":"ev","exec_bound_ms":100,"deadline_in_ms":30000,"simulate_actual_ms":5}`)
	time.Sleep(300 * time.Millisecond)

	code, body := getJSON(t, ts.URL+"/events")
	if code != 200 {
		t.Fatalf("events = %d", code)
	}
	events, ok := body["events"].([]any)
	if !ok || len(events) == 0 {
		t.Fatalf("no events returned: %v", body)
	}
	first := events[0].(map[string]any)
	if first["seq"] != float64(1) || first["type"] != "submitted" {
		t.Fatalf("first event = %v, want seq=1 type=submitted", first)
	}
}
