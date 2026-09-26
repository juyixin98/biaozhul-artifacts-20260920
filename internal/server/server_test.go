package server

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graceful-shutdown/internal/clock"
	"graceful-shutdown/internal/fakesvc"
	"graceful-shutdown/internal/ledger"
	"graceful-shutdown/internal/resources"
	"graceful-shutdown/internal/shutdown"
)

type fixture struct {
	ts    *httptest.Server
	coord *shutdown.Coordinator
}

func newFixture(t *testing.T, drainTimeout time.Duration) *fixture {
	t.Helper()
	clk := clock.Real{}
	led := ledger.New()
	reg := resources.NewRegistry()
	db := fakesvc.NewFakeDB(clk)
	queue := fakesvc.NewFakeQueue(clk)
	reg.Register(db)
	reg.Register(queue)
	coord := shutdown.New(clk, drainTimeout, led, reg)
	srv := New(coord, clk, db, queue)
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return &fixture{ts: ts, coord: coord}
}

func (f *fixture) get(t *testing.T, path string) (int, []byte) {
	t.Helper()
	resp, err := http.Get(f.ts.URL + path)
	if err != nil {
		t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, body
}

func (f *fixture) post(t *testing.T, path string) (int, []byte) {
	t.Helper()
	resp, err := http.Post(f.ts.URL+path, "application/json", nil)
	if err != nil {
		t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, body
}

func TestHealthAndReadySeparated(t *testing.T) {
	f := newFixture(t, time.Second)
	if code, _ := f.get(t, "/healthz"); code != http.StatusOK {
		t.Errorf("healthz = %d, want 200", code)
	}
	if code, _ := f.get(t, "/readyz"); code != http.StatusOK {
		t.Errorf("readyz = %d, want 200", code)
	}
	f.post(t, "/shutdown")
	<-f.coord.Done()
	if code, _ := f.get(t, "/healthz"); code != http.StatusOK {
		t.Errorf("healthz after shutdown = %d, want 200 (liveness independent)", code)
	}
	if code, _ := f.get(t, "/readyz"); code != http.StatusServiceUnavailable {
		t.Errorf("readyz after shutdown = %d, want 503", code)
	}
}

func TestInterleavedShutdownScenario(t *testing.T) {
	f := newFixture(t, 400*time.Millisecond)

	type result struct {
		code int
		body string
	}
	results := map[string]chan result{
		"long":       make(chan result, 1),
		"stream":     make(chan result, 1),
		"too-long":   make(chan result, 1),
		"longstream": make(chan result, 1),
	}
	fire := func(label, path string) {
		go func() {
			resp, err := http.Get(f.ts.URL + path)
			if err != nil {
				results[label] <- result{code: -1, body: err.Error()}
				return
			}
			defer resp.Body.Close()
			body, _ := io.ReadAll(resp.Body)
			results[label] <- result{code: resp.StatusCode, body: string(body)}
		}()
	}

	// In-flight before shutdown: short work, short stream, stuck work, long stream.
	fire("long", "/work?ms=100&q=long")
	fire("stream", "/stream?chunks=3&interval_ms=50")
	fire("too-long", "/work?ms=30000&q=stuck")
	fire("longstream", "/stream?chunks=100&interval_ms=100")

	// Background task accepted before shutdown.
	if code, _ := f.post(t, "/task?msg=job1"); code != http.StatusAccepted {
		t.Fatalf("task before shutdown = %d, want 202", code)
	}

	time.Sleep(80 * time.Millisecond) // let everything get accepted

	// Repeated shutdown signals interleaved with in-flight work.
	if code, _ := f.post(t, "/shutdown"); code != http.StatusAccepted {
		t.Fatalf("shutdown = %d, want 202", code)
	}
	if code, _ := f.post(t, "/shutdown"); code != http.StatusAccepted {
		t.Fatalf("duplicate shutdown = %d, want 202", code)
	}

	// After stop-accepting: readiness fails, liveness holds, new work rejected.
	if code, _ := f.get(t, "/readyz"); code != http.StatusServiceUnavailable {
		t.Errorf("readyz during drain = %d, want 503", code)
	}
	if code, _ := f.get(t, "/healthz"); code != http.StatusOK {
		t.Errorf("healthz during drain = %d, want 200", code)
	}
	if code, _ := f.get(t, "/work?ms=1"); code != http.StatusServiceUnavailable {
		t.Errorf("new request during drain = %d, want 503", code)
	}
	if code, _ := f.post(t, "/task?msg=late"); code != http.StatusServiceUnavailable {
		t.Errorf("new task during drain = %d, want 503", code)
	}

	// Collect in-flight outcomes.
	got := map[string]result{}
	for label := range results {
		select {
		case r := <-results[label]:
			got[label] = r
		case <-time.After(5 * time.Second):
			t.Fatalf("request %q never resolved", label)
		}
	}

	if got["long"].code != http.StatusOK || !strings.Contains(got["long"].body, `"completed"`) {
		t.Errorf("long request: got %d %s, want 200 completed", got["long"].code, got["long"].body)
	}
	if got["stream"].code != http.StatusOK || !strings.Contains(got["stream"].body, "chunk 3/3") {
		t.Errorf("stream: got %d %q, want 200 with final chunk", got["stream"].code, got["stream"].body)
	}
	if got["too-long"].code != StatusClientClosed || !strings.Contains(got["too-long"].body, `"cancelled"`) {
		t.Errorf("too-long: got %d %s, want 499 cancelled", got["too-long"].code, got["too-long"].body)
	}
	if got["longstream"].code != http.StatusOK || !strings.Contains(got["longstream"].body, "cancelled") {
		t.Errorf("long stream: got %d %q, want 200 with cancelled marker", got["longstream"].code, got["longstream"].body)
	}

	<-f.coord.Done()
	st := f.coord.State()

	if !st.DrainTimedOut {
		t.Error("drain should have timed out (stuck request present)")
	}
	if st.ShutdownSignals != 2 {
		t.Errorf("shutdown signals = %d, want 2", st.ShutdownSignals)
	}
	if st.RejectedRequests != 1 || st.RejectedTasks != 1 {
		t.Errorf("rejections = %d requests / %d tasks, want 1/1", st.RejectedRequests, st.RejectedTasks)
	}
	if len(st.CloseOrder) != 2 || st.CloseOrder[0].Name != "fake-queue" || st.CloseOrder[1].Name != "fake-db" {
		t.Errorf("close order = %+v, want fake-queue then fake-db", st.CloseOrder)
	}

	// Every accepted unit of work has a terminal outcome.
	var completed, cancelled int
	for _, e := range st.Ledger {
		switch e.Outcome {
		case ledger.OutcomeCompleted:
			completed++
		case ledger.OutcomeCancelled:
			cancelled++
		default:
			t.Errorf("ledger entry %+v has no terminal outcome", e)
		}
	}
	if completed == 0 || cancelled == 0 {
		t.Errorf("want both completed and cancelled outcomes, got %d/%d", completed, cancelled)
	}
	fmt.Printf("ledger: %d entries (%d completed, %d cancelled)\n", len(st.Ledger), completed, cancelled)
}

func TestStateEndpointShape(t *testing.T) {
	f := newFixture(t, 100*time.Millisecond)
	f.post(t, "/shutdown")
	<-f.coord.Done()
	code, body := f.get(t, "/state")
	if code != http.StatusOK {
		t.Fatalf("state = %d", code)
	}
	var st shutdown.State
	if err := json.Unmarshal(body, &st); err != nil {
		t.Fatalf("state not JSON-decodable: %v", err)
	}
	if st.Phase != shutdown.PhaseDone {
		t.Errorf("phase = %s, want done", st.Phase)
	}
	if st.Accepting {
		t.Error("accepting should be false after shutdown")
	}
}
