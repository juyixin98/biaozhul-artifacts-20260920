package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"tailsampler/internal/sampler"
)

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (f *fakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

func (f *fakeClock) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}

func newTestServer(t *testing.T) (*httptest.Server, *sampler.Sampler, *fakeClock) {
	t.Helper()
	clock := &fakeClock{t: time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)}
	s := sampler.New(sampler.Config{
		DecisionWait:       10 * time.Second,
		LatencyThresholdMs: 500,
		BudgetKeepsPerMin:  2,
		MaxInflightTraces:  100,
		DecisionTTL:        time.Hour,
	}, nil, clock)
	return httptest.NewServer(NewServer(s)), s, clock
}

func postSpans(t *testing.T, base string, spans []sampler.Span) map[string]sampler.IngestStatus {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"spans": spans})
	resp, err := http.Post(base+"/v1/spans", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("ingest status %d", resp.StatusCode)
	}
	var out struct {
		Accepted int                             `json:"accepted"`
		Traces   map[string]sampler.IngestStatus `json:"traces"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Accepted != len(spans) {
		t.Fatalf("accepted=%d want %d", out.Accepted, len(spans))
	}
	return out.Traces
}

func getJSON(t *testing.T, url string, wantStatus int, out any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != wantStatus {
		t.Fatalf("GET %s: status %d want %d", url, resp.StatusCode, wantStatus)
	}
	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			t.Fatal(err)
		}
	}
}

func span(trace, id, parent, status string, start, dur int64) sampler.Span {
	return sampler.Span{TraceID: trace, SpanID: id, ParentID: parent,
		Service: "svc", Name: "op", StartUnixMs: start, DurationMs: dur, Status: status}
}

func TestEndToEnd(t *testing.T) {
	ts, s, clock := newTestServer(t)
	defer ts.Close()

	st := postSpans(t, ts.URL, []sampler.Span{
		span("e2e-err", "r", "", "ok", 1000, 30),
		span("e2e-err", "c", "r", "error", 1005, 10),
		span("e2e-ok", "r", "", "ok", 1000, 20),
	})
	if st["e2e-err"] != sampler.StatusPending || st["e2e-ok"] != sampler.StatusPending {
		t.Fatalf("expected pending: %v", st)
	}

	clock.Advance(11 * time.Second)
	s.DecideDue()

	var dErr sampler.Decision
	getJSON(t, ts.URL+"/v1/decisions/e2e-err", http.StatusOK, &dErr)
	if !dErr.Keep || dErr.Incomplete {
		t.Fatalf("unexpected decision: %+v", dErr)
	}
	var dOK sampler.Decision
	getJSON(t, ts.URL+"/v1/decisions/e2e-ok", http.StatusOK, &dOK)
	if dOK.Keep {
		t.Fatalf("expected drop: %+v", dOK)
	}

	// Kept trace spans are queryable; dropped are not.
	var tr struct {
		Spans []sampler.Span `json:"spans"`
	}
	getJSON(t, ts.URL+"/v1/traces/e2e-err", http.StatusOK, &tr)
	if len(tr.Spans) != 2 {
		t.Fatalf("expected 2 spans, got %d", len(tr.Spans))
	}
	getJSON(t, ts.URL+"/v1/traces/e2e-ok", http.StatusNotFound, nil)

	// Late span on the dropped trace keeps the drop verdict.
	st = postSpans(t, ts.URL, []sampler.Span{span("e2e-ok", "late", "r", "error", 1010, 5)})
	if st["e2e-ok"] != sampler.StatusDrop {
		t.Fatalf("late span verdict changed: %v", st)
	}
	getJSON(t, ts.URL+"/v1/decisions/e2e-ok", http.StatusOK, &dOK)
	if !dOK.Incomplete || dOK.LateSpans != 1 {
		t.Fatalf("late span not recorded: %+v", dOK)
	}

	// List filters.
	var list struct {
		Decisions []sampler.Decision `json:"decisions"`
	}
	getJSON(t, ts.URL+"/v1/decisions?keep=true", http.StatusOK, &list)
	if len(list.Decisions) != 1 || list.Decisions[0].TraceID != "e2e-err" {
		t.Fatalf("keep filter: %+v", list.Decisions)
	}
	getJSON(t, ts.URL+"/v1/decisions?incomplete=true", http.StatusOK, &list)
	if len(list.Decisions) != 1 || list.Decisions[0].TraceID != "e2e-ok" {
		t.Fatalf("incomplete filter: %+v", list.Decisions)
	}

	var stats sampler.Stats
	getJSON(t, ts.URL+"/v1/stats", http.StatusOK, &stats)
	if stats.SpansReceived != 4 || stats.TracesKept != 1 || stats.TracesDropped != 1 || stats.LateSpans != 1 {
		t.Fatalf("stats: %+v", stats)
	}
}

func TestIngestValidation(t *testing.T) {
	ts, _, _ := newTestServer(t)
	defer ts.Close()

	resp, err := http.Post(ts.URL+"/v1/spans", "application/json", strings.NewReader(`{"spans":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("empty spans: status %d", resp.StatusCode)
	}

	resp, err = http.Post(ts.URL+"/v1/spans", "application/json", strings.NewReader(`{"spans":[{"span_id":"x"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing trace_id: status %d", resp.StatusCode)
	}

	resp, err = http.Post(ts.URL+"/v1/spans", "application/json", strings.NewReader(`not-json`))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json: status %d", resp.StatusCode)
	}
}
