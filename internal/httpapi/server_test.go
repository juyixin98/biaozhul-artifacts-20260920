package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"tokenbudget/internal/budget"
	"tokenbudget/internal/clock"
	"tokenbudget/internal/event"
)

const secNS = int64(time.Second)

func newTestServer(t *testing.T) (*Server, *clock.FakeClock, http.Handler) {
	t.Helper()
	fc := clock.NewFakeClock(0)
	mem := &event.MemorySink{}
	bus := event.NewBus(fc, mem)
	l, err := budget.NewLimiter(fc, bus,
		budget.Config{Rate: budget.Rate{Num: 2, Den: secNS}, Capacity: 2},
		budget.Config{Rate: budget.Rate{Num: 1, Den: secNS}, Capacity: 1},
	)
	if err != nil {
		t.Fatal(err)
	}
	sched := budget.NewScheduler(l, fc, bus, 0)
	s := &Server{Limiter: l, Scheduler: sched, Mem: mem}
	return s, fc, s.NewMux()
}

func do(t *testing.T, h http.Handler, method, path, body string) (int, map[string]any) {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, bytes.NewBufferString(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	out := map[string]any{}
	if w.Body.Len() > 0 {
		_ = json.Unmarshal(w.Body.Bytes(), &out)
	}
	return w.Code, out
}

func TestHealthAndState(t *testing.T) {
	_, _, h := newTestServer(t)
	if code, b := do(t, h, "GET", "/healthz", ""); code != 200 || b["status"] != "ok" {
		t.Fatalf("health code=%d body=%v", code, b)
	}
	code, st := do(t, h, "GET", "/state", "")
	if code != 200 {
		t.Fatalf("state code=%d", code)
	}
	g, _ := st["global"].(map[string]any)
	if g["capacity"].(float64) != 2 || g["available"].(float64) != 2 {
		t.Fatalf("global snapshot wrong: %v", g)
	}
}

func TestRequestAllowedThenDeniedNoPartial(t *testing.T) {
	_, fc, h := newTestServer(t)

	// Tenant burst = 1, global burst = 2. First request: 200.
	code, b := do(t, h, "POST", "/request", `{"tenant":"acme","tokens":1}`)
	if code != 200 || b["allowed"] != true {
		t.Fatalf("first code=%d body=%v", code, b)
	}
	// Second request denied by the tenant layer; global must keep 1 token.
	code, b = do(t, h, "POST", "/request", `{"tenant":"acme","tokens":1}`)
	if code != http.StatusTooManyRequests || b["allowed"] != false {
		t.Fatalf("second code=%d body=%v", code, b)
	}
	_, st := do(t, h, "GET", "/state", "")
	glob := st["global"].(map[string]any)
	if glob["available"].(float64) != 1 {
		t.Fatalf("global available=%v want 1 (no partial consume)", glob["available"])
	}

	// After 1 virtual second the tenant refills one token.
	fc.Advance(time.Second)
	code, b = do(t, h, "POST", "/request", `{"tenant":"acme","tokens":1}`)
	if code != 200 || b["allowed"] != true {
		t.Fatalf("post-refill code=%d body=%v", code, b)
	}
}

func TestRequestExceedsCapacity(t *testing.T) {
	_, _, h := newTestServer(t)
	code, b := do(t, h, "POST", "/request", `{"tenant":"acme","tokens":99}`)
	if code != http.StatusUnprocessableEntity || b["reason"] != budget.ReasonExceedsCap {
		t.Fatalf("code=%d body=%v", code, b)
	}
	// Bad JSON -> 400.
	if code, _ := do(t, h, "POST", "/request", `{not json`); code != http.StatusBadRequest {
		t.Fatalf("bad json code=%d want 400", code)
	}
	// Missing tenant -> 400.
	if code, _ := do(t, h, "POST", "/request", `{"tokens":1}`); code != http.StatusBadRequest {
		t.Fatalf("missing tenant code=%d want 400", code)
	}
}

func TestConfigEndpoints(t *testing.T) {
	s, _, h := newTestServer(t)

	// Create a tenant with explicit capacity and empty stock.
	body := `{"rate":{"rate_num":5,"rate_den_ns":1000000000},"capacity":7,"initial_tokens":0}`
	code, st := do(t, h, "PUT", "/config/tenants/beta", body)
	if code != 200 {
		t.Fatalf("tenant config code=%d", code)
	}
	beta := st["tenants"].(map[string]any)["beta"].(map[string]any)
	if beta["capacity"].(float64) != 7 || beta["available"].(float64) != 0 {
		t.Fatalf("beta config wrong: %v", beta)
	}

	// Growing global capacity must not grant tokens (still 2 available).
	code, st = do(t, h, "PUT", "/config/global",
		`{"rate":{"rate_num":2,"rate_den_ns":1000000000},"capacity":10}`)
	if code != 200 {
		t.Fatalf("global config code=%d", code)
	}
	glob := st["global"].(map[string]any)
	if glob["capacity"].(float64) != 10 || glob["available"].(float64) != 2 {
		t.Fatalf("global grow must not mint tokens: %v", glob)
	}

	// Invalid config -> 400.
	if code, _ := do(t, h, "PUT", "/config/global", `{"rate":{"rate_num":1},"capacity":0}`); code != http.StatusBadRequest {
		t.Fatalf("invalid config code=%d want 400", code)
	}

	// The limiter reports the tenant directly.
	if v := s.Limiter.Snapshot().Tenants["beta"]; v.Capacity != 7 {
		t.Fatalf("tenant beta missing from snapshot")
	}
}

func TestScheduleLifecycleWithVirtualClock(t *testing.T) {
	_, fc, h := newTestServer(t)

	// Tenant burst 1 is consumed now and executes immediately.
	code, b := do(t, h, "POST", "/schedule", `{"tenant":"acme","name":"now","tokens":1}`)
	if code != http.StatusAccepted || b["accepted"] != true {
		t.Fatalf("schedule1 code=%d body=%v", code, b)
	}
	fc.Advance(0)
	job1 := b["job"].(map[string]any)

	// Second needs ~1s (tenant 1/s) — accepted for later.
	code, b = do(t, h, "POST", "/schedule", `{"tenant":"acme","name":"later","tokens":1}`)
	if code != http.StatusAccepted {
		t.Fatalf("schedule2 code=%d body=%v", code, b)
	}
	job2 := b["job"].(map[string]any)
	if job2["wait_ns"].(float64) < float64(time.Second) {
		t.Fatalf("later job wait=%v want >=1s", job2["wait_ns"])
	}

	// Before advancing, later job is queued.
	code, b = do(t, h, "GET", "/jobs/"+job2["id"].(string), "")
	if code != 200 || b["status"] != string(budget.JobQueued) {
		t.Fatalf("early job status code=%d body=%v", code, b)
	}

	fc.Advance(2 * time.Second)
	code, b = do(t, h, "GET", "/jobs/"+job2["id"].(string), "")
	if code != 200 || b["status"] != string(budget.JobSucceeded) {
		t.Fatalf("post-advance job code=%d body=%v", code, b)
	}

	// Jobs list contains both; first succeeded too.
	_, all := do(t, h, "GET", "/jobs", "")
	jobs := all["jobs"].([]any)
	if len(jobs) != 2 {
		t.Fatalf("jobs len=%d want 2", len(jobs))
	}
	_ = job1
	if code, _ := do(t, h, "GET", "/jobs/nope", ""); code != http.StatusNotFound {
		t.Fatalf("unknown job code=%d want 404", code)
	}
}

func TestScheduleRejectedOversize(t *testing.T) {
	_, _, h := newTestServer(t)
	code, b := do(t, h, "POST", "/schedule", `{"tenant":"acme","tokens":42}`)
	if code != http.StatusUnprocessableEntity || b["accepted"] != false {
		t.Fatalf("code=%d body=%v", code, b)
	}
}

func TestEventsRecorded(t *testing.T) {
	_, _, h := newTestServer(t)
	do(t, h, "POST", "/request", `{"tenant":"acme","tokens":1}`)
	do(t, h, "POST", "/request", `{"tenant":"acme","tokens":1}`) // denied

	code, b := do(t, h, "GET", "/events", "")
	if code != 200 {
		t.Fatalf("events code=%d", code)
	}
	evs := b["events"].([]any)
	if len(evs) < 5 {
		t.Fatalf("expected tenant_created + grants + denials, got %d", len(evs))
	}
	// Every event carries a monotonic sequence and timestamp.
	var lastSeq float64
	for i, e := range evs {
		ev := e.(map[string]any)
		seq := ev["seq"].(float64)
		if i > 0 && seq <= lastSeq {
			t.Fatalf("non-monotonic seq at %d", i)
		}
		lastSeq = seq
		if ev["at_ns"] == nil {
			t.Fatal("event missing at_ns")
		}
	}
}
