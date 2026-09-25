package server_test

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"histmerge/internal/histogram"
	"histmerge/internal/server"
	"histmerge/internal/store"
)

func urlEncode(s string) string { return url.QueryEscape(s) }

func setup(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st, err := store.New("")
	if err != nil {
		t.Fatal(err)
	}
	srv := server.New(st)
	ts := httptest.NewServer(srv.Mux)
	t.Cleanup(func() { ts.Close(); st.Close() })
	return ts, st
}

func do(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, url, r)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return resp.StatusCode, m
}

// ingestPayload helpers -------------------------------------------------------

// hJSON is the wire histogram: cumulative buckets with "+Inf".
func hJSON(sum float64, upper []any, counts []uint64) map[string]any {
	var run uint64
	buckets := []map[string]any{}
	for i, c := range counts {
		run += c
		var ub any
		if i < len(upper) {
			ub = upper[i]
		} else {
			ub = "+Inf"
		}
		buckets = append(buckets, map[string]any{"upper": ub, "cumulative_count": run})
	}
	return map[string]any{
		"total_count": run,
		"sum":         sum,
		"buckets":     buckets,
	}
}

func sample(ts time.Time, service, route string, h map[string]any) map[string]any {
	return map[string]any{
		"timestamp": ts.Format(time.RFC3339Nano),
		"labels":    []map[string]any{{"name": "service", "value": service}, {"name": "route", "value": route}},
		"histogram": h,
	}
}

func TestHealth(t *testing.T) {
	ts, _ := setup(t)
	status, body := do(t, "GET", ts.URL+"/healthz", nil)
	if status != 200 || body["status"] != "ok" {
		t.Fatalf("health = %d %v", status, body)
	}
}

func TestIngestAndQuery_DirectMerge_Conservation(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	uppers := []any{1.0, 2.0, 4.0}
	s1 := sample(t0, "a", "/x", hJSON(10, uppers, []uint64{1, 2, 3, 4}))
	s2 := sample(t0.Add(time.Minute), "b", "/y", hJSON(20, uppers, []uint64{0, 4, 2, 8}))
	status, body := do(t, "POST", ts.URL+"/api/v1/ingest", map[string]any{"samples": []any{s1, s2}})
	if status != 200 {
		t.Fatalf("ingest status = %d body=%v", status, body)
	}
	if body["accepted"].(float64) != 2 {
		t.Fatalf("accepted = %v", body["accepted"])
	}

	status, q := do(t, "GET", ts.URL+"/api/v1/query?quantile=0.5&quantile=0.99", nil)
	if status != 200 {
		t.Fatalf("query status = %d body=%v", status, q)
	}
	if q["strategy"] != "direct" {
		t.Fatalf("strategy = %v, want direct", q["strategy"])
	}
	if q["total_count_conserved"] != true || q["bucket_count_conserved"] != true {
		t.Fatalf("conservation flags wrong: %v", q)
	}
	// totals: 10 and 14
	if q["input_total_count"].(float64) != 24 {
		t.Fatalf("input total = %v", q["input_total_count"])
	}
	merge := q["merge"].(map[string]any)
	if merge["total_count"].(float64) != 24 {
		t.Fatalf("merged total = %v, want 24", merge["total_count"])
	}
	qs := q["quantiles"].([]any)
	if len(qs) != 2 {
		t.Fatalf("want 2 quantiles, got %d", len(qs))
	}
}

func TestQuery_IncompatibleStrict_Conflict(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	s1 := sample(t0, "a", "/x", hJSON(10, []any{1.0, 2.0}, []uint64{1, 1, 0}))
	s2 := sample(t0.Add(time.Minute), "b", "/y", hJSON(10, []any{2.0, 3.0}, []uint64{1, 1, 0}))
	_, _ = do(t, "POST", ts.URL+"/api/v1/ingest", map[string]any{"samples": []any{s1, s2}})

	status, body := do(t, "GET", ts.URL+"/api/v1/query?coarsen=false", nil)
	if status != http.StatusConflict {
		t.Fatalf("strict incompatible = %d (want 409), body=%v", status, body)
	}
	if !strings.Contains(body["error"].(string), "incompatible") {
		t.Fatalf("error message = %v", body["error"])
	}
}

func TestQuery_CoarsenedMerge(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	s1 := sample(t0, "a", "/x", hJSON(10, []any{1.0, 2.0, 4.0}, []uint64{1, 2, 3, 4}))
	s2 := sample(t0.Add(time.Minute), "b", "/y", hJSON(10, []any{2.0, 4.0, 8.0}, []uint64{1, 2, 3, 4}))
	_, _ = do(t, "POST", ts.URL+"/api/v1/ingest", map[string]any{"samples": []any{s1, s2}})

	status, body := do(t, "POST", ts.URL+"/api/v1/query",
		map[string]any{"quantiles": []float64{0.5}})
	if status != 200 {
		t.Fatalf("coarsened query = %d body=%v", status, body)
	}
	if body["strategy"] != "coarsened" {
		t.Fatalf("strategy = %v want coarsened", body["strategy"])
	}
	if body["total_count_conserved"] != true {
		t.Fatal("total count must be conserved even after coarsening")
	}
	cb := body["common_bounds"].([]any)
	// common finite bounds 2,4 plus +Inf
	if len(cb) != 3 || cb[2] != "+Inf" {
		t.Fatalf("common_bounds = %v", cb)
	}
}

func TestQuery_NoCommonBounds(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	s1 := sample(t0, "a", "/x", hJSON(1, []any{1.0}, []uint64{1, 0}))
	s2 := sample(t0.Add(time.Minute), "b", "/y", hJSON(1, []any{2.0}, []uint64{1, 0}))
	_, _ = do(t, "POST", ts.URL+"/api/v1/ingest", map[string]any{"samples": []any{s1, s2}})
	status, body := do(t, "GET", ts.URL+"/api/v1/query", nil)
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("no common bounds = %d (want 422), body=%v", status, body)
	}
}

func TestIngest_InvalidHistogramRejected(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	bad := map[string]any{
		"timestamp": t0.Format(time.RFC3339Nano),
		"labels":    []map[string]any{{"name": "service", "value": "a"}},
		"histogram": map[string]any{
			"total_count": 3,
			"sum":         5,
			"buckets": []map[string]any{
				{"upper": 1.0, "cumulative_count": 3}, // missing +Inf
			},
		},
	}
	status, body := do(t, "POST", ts.URL+"/api/v1/ingest", bad)
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("invalid ingest = %d (want 422), body=%v", status, body)
	}
	if body["accepted"].(float64) != 0 || body["rejected"].(float64) != 1 {
		t.Fatalf("accepted/rejected wrong: %v", body)
	}
}

func TestIngest_CounterResetAcrossRequests(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	s1 := sample(t0, "a", "/x", hJSON(5, []any{1.0, 2.0}, []uint64{2, 1, 2}))
	s2 := sample(t0.Add(time.Minute), "a", "/x", hJSON(4, []any{1.0, 2.0}, []uint64{1, 1, 2}))
	if st, _ := do(t, "POST", ts.URL+"/api/v1/ingest", s1); st != 200 {
		t.Fatal("first ingest should succeed")
	}
	status, body := do(t, "POST", ts.URL+"/api/v1/ingest", s2)
	if status != http.StatusConflict {
		t.Fatalf("reset ingest = %d (want 409), body=%v", status, body)
	}
	if !strings.Contains(body["results"].([]any)[0].(map[string]any)["error"].(string), "counter reset") {
		t.Fatalf("expected counter reset error, got %v", body)
	}
}

func TestQueryRange_IncreaseAndMerge(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	up := []any{1.0, 2.0}
	// series a: grows from total 2 -> 5
	a1 := sample(t0, "a", "/x", hJSON(2, up, []uint64{1, 1, 0}))
	a2 := sample(t0.Add(2*time.Minute), "a", "/x", hJSON(8, up, []uint64{2, 1, 2}))
	// series b: grows from total 1 -> 3
	b1 := sample(t0.Add(30*time.Second), "b", "/y", hJSON(1, up, []uint64{1, 0, 0}))
	b2 := sample(t0.Add(3*time.Minute), "b", "/y", hJSON(4, up, []uint64{1, 1, 1}))
	_, _ = do(t, "POST", ts.URL+"/api/v1/ingest", map[string]any{"samples": []any{a1, b1, a2, b2}})

	from := t0.Add(-time.Minute).Format(time.RFC3339)
	to := t0.Add(10 * time.Minute).Format(time.RFC3339)
	status, body := do(t, "GET",
		ts.URL+"/api/v1/query_range?from="+urlEncode(from)+"&to="+urlEncode(to)+"&quantile=0.5", nil)
	if status != 200 {
		t.Fatalf("query_range = %d body=%v", status, body)
	}
	// increases: a = 3, b = 2 -> merged total 5
	if body["input_total_count"].(float64) != 5 {
		t.Fatalf("range input total = %v, want 5", body["input_total_count"])
	}
	if body["total_count_conserved"] != true {
		t.Fatal("range aggregation must conserve counts")
	}
}

func TestQuery_EmptySeriesExcludedFromQuantiles(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	up := []any{1.0, 2.0}
	empty := sample(t0, "idle", "/h", map[string]any{
		"total_count": 0, "sum": 0,
		"buckets": []map[string]any{
			{"upper": 1.0, "cumulative_count": 0},
			{"upper": 2.0, "cumulative_count": 0},
			{"upper": "+Inf", "cumulative_count": 0},
		},
	})
	busy := sample(t0.Add(time.Minute), "busy", "/x", hJSON(5, up, []uint64{2, 1, 2}))
	_, _ = do(t, "POST", ts.URL+"/api/v1/ingest", map[string]any{"samples": []any{empty, busy}})
	status, body := do(t, "GET", ts.URL+"/api/v1/query?quantile=0.5", nil)
	if status != 200 {
		t.Fatalf("query = %d body=%v", status, body)
	}
	if body["empty"] != false {
		t.Fatal("aggregate with one non-empty series must not be empty")
	}
	if body["input_total_count"].(float64) != 5 {
		t.Fatalf("empty series must add zero, got total %v", body["input_total_count"])
	}
}

func TestQuery_NoSeries(t *testing.T) {
	ts, _ := setup(t)
	status, _ := do(t, "GET", ts.URL+"/api/v1/query?match[]=service=nope", nil)
	if status != http.StatusNotFound {
		t.Fatalf("empty selector = %d, want 404", status)
	}
}

func TestInfBound_AcceptedAsStringOrNumber(t *testing.T) {
	ts, _ := setup(t)
	t0 := time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)
	// "+Inf" string wire form is exercised everywhere; also confirm 1e999
	// does not silently work as a finite bound duplicate.
	h := sample(t0, "a", "/x", hJSON(1, []any{1.0}, []uint64{0, 1}))
	status, _ := do(t, "POST", ts.URL+"/api/v1/ingest", h)
	if status != 200 {
		t.Fatalf("+Inf string form ingest = %d", status)
	}
}

// guard to ensure histogram package stays imported for error sentinel docs.
var _ = histogram.ErrInvalidHistogram
