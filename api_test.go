package tailsampling

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func newTestServer(t *testing.T) (*httptest.Server, *Aggregator, *fakeClock, Config) {
	t.Helper()
	cfg := testConfig()
	cfg.BudgetCapacity = 5
	agg, clk, _ := newTestAggregator(t, cfg)
	srv := httptest.NewServer(NewServer(agg).Handler())
	t.Cleanup(srv.Close)
	return srv, agg, clk, cfg
}

func TestHTTPIngestAndQueryDecision(t *testing.T) {
	srv, agg, clk, cfg := newTestServer(t)
	base := clk.Now().UnixMilli()
	body := ingestPayload{Spans: []Span{
		span("http1", "http1-r", "", StatusError, base, 1200),
		span("http1", "http1-c", "http1-r", StatusOK, base+1, 50),
	}}
	raw, _ := json.Marshal(body)
	resp, err := http.Post(srv.URL+"/v1/spans", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	var report IngestReport
	if err := json.NewDecoder(resp.Body).Decode(&report); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if report.Accepted != 2 {
		t.Fatalf("accepted=%d", report.Accepted)
	}

	advance(clk, agg, cfg.WaitWindow+time.Millisecond)
	dresp, err := http.Get(srv.URL + "/v1/decisions/http1")
	if err != nil {
		t.Fatal(err)
	}
	if dresp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", dresp.StatusCode)
	}
	var d Decision
	if err := json.NewDecoder(dresp.Body).Decode(&d); err != nil {
		t.Fatal(err)
	}
	dresp.Body.Close()
	if !d.Kept || d.Policy != "error" {
		t.Fatalf("unexpected decision: %+v", d)
	}
}

func TestHTTPDecisionNotFoundAnd400s(t *testing.T) {
	srv, _, _, _ := newTestServer(t)

	r, _ := http.Get(srv.URL + "/v1/decisions/unknown")
	if r.StatusCode != http.StatusNotFound {
		t.Fatalf("want 404, got %d", r.StatusCode)
	}
	r.Body.Close()

	r, _ = http.Post(srv.URL+"/v1/spans", "application/json",
		strings.NewReader(`{"spans":[{"span_id":"x","start_time_ms":1}]}`))
	if r.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 for missing trace_id, got %d", r.StatusCode)
	}
	r.Body.Close()

	r, _ = http.Post(srv.URL+"/v1/spans", "application/json", strings.NewReader(`{"spans":[]}`))
	if r.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 for empty batch, got %d", r.StatusCode)
	}
	r.Body.Close()

	r, _ = http.Post(srv.URL+"/v1/spans", "application/json", strings.NewReader(`{nope`))
	if r.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 for invalid json, got %d", r.StatusCode)
	}
	r.Body.Close()
}

func TestHTTPStatsAndListEndpoints(t *testing.T) {
	srv, agg, clk, cfg := newTestServer(t)
	base := clk.Now().UnixMilli()
	post := func(p any) {
		raw, _ := json.Marshal(p)
		r, err := http.Post(srv.URL+"/v1/spans", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
	}
	post(ingestPayload{Spans: []Span{span("z1", "r", "", StatusError, base, 10)}})
	post(ingestPayload{Spans: []Span{span("z2", "r", "", StatusOK, base, 10)}})
	advance(clk, agg, cfg.WaitWindow+time.Millisecond)

	r, _ := http.Get(srv.URL + "/v1/stats")
	var st Stats
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		t.Fatal(err)
	}
	r.Body.Close()
	if st.TotalDecided != 2 || st.Kept != 1 || st.Dropped != 1 {
		t.Fatalf("stats wrong: %+v", st)
	}

	r, _ = http.Get(srv.URL + "/v1/decisions?kept=true")
	var kept struct {
		Count int `json:"count"`
	}
	if err := json.NewDecoder(r.Body).Decode(&kept); err != nil {
		t.Fatal(err)
	}
	r.Body.Close()
	if kept.Count != 1 {
		t.Fatalf("kept filter count=%d", kept.Count)
	}

	r, _ = http.Get(srv.URL + "/v1/decisions?limit=1")
	var one struct {
		Count int `json:"count"`
	}
	json.NewDecoder(r.Body).Decode(&one)
	r.Body.Close()
	if one.Count != 1 {
		t.Fatalf("limit count=%d", one.Count)
	}
}

func TestHTTPAdminFlushFinalizesIncomplete(t *testing.T) {
	srv, _, _, _ := newTestServer(t)
	base := time.Now().UnixMilli()
	raw, _ := json.Marshal(ingestPayload{Spans: []Span{span("q", "c", "missing", StatusOK, base, 10)}})
	r, err := http.Post(srv.URL+"/v1/spans", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	r.Body.Close()

	r, _ = http.Post(srv.URL+"/admin/flush", "application/json", nil)
	if r.StatusCode != http.StatusOK {
		t.Fatalf("flush status=%d", r.StatusCode)
	}
	var out struct {
		Finalized int `json:"finalized"`
	}
	json.NewDecoder(r.Body).Decode(&out)
	r.Body.Close()
	if out.Finalized != 1 {
		t.Fatalf("finalized=%d", out.Finalized)
	}

	r, _ = http.Get(srv.URL + "/v1/decisions/q")
	var d Decision
	json.NewDecoder(r.Body).Decode(&d)
	r.Body.Close()
	if d.Complete || d.ReasonCode != ReasonForcedIncomplete {
		t.Fatalf("flush decision wrong: %+v", d)
	}
}
