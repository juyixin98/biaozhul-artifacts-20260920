package api_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"cardinalitybudget/internal/api"
	"cardinalitybudget/internal/store"
)

func newTestServer(t *testing.T, flusher api.Flusher) (*httptest.Server, *store.Store) {
	t.Helper()
	cfg := store.Config{
		MaxSeriesPerMetric: 3,
		MaxMetricNames:     3,
		MaxMetricNameLen:   32,
		MaxLabelKeys:       4,
		MaxLabelKeyLen:     16,
		MaxLabelValueLen:   8,
	}
	st, err := store.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	srv := api.NewServer(st, nil, flusher)
	return httptest.NewServer(srv.Handler()), st
}

func post(t *testing.T, url string, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func get(t *testing.T, url string) (int, map[string]any) {
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

func TestIngestAndQueryEndpoints(t *testing.T) {
	srv, _ := newTestServer(t, nil)
	defer srv.Close()

	// Two distinct combos fit, a third unique combo overflows.
	status, out := post(t, srv.URL+"/api/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "http_requests", "labels": map[string]string{"path": "/a"}, "value": 1},
			{"metric": "http_requests", "labels": map[string]string{"path": "/b"}, "value": 2},
			{"metric": "http_requests", "labels": map[string]string{"path": "/c"}, "value": 3},
			{"metric": "http_requests", "labels": map[string]string{"path": "/x"}, "value": 4},
			{"metric": "", "labels": map[string]string{}, "value": 1},
		},
	})
	if status != http.StatusOK {
		t.Fatalf("status %d: %v", status, out)
	}
	if out["accepted"] != float64(4) || out["rejected"] != float64(1) || out["overflow"] != float64(1) {
		t.Fatalf("ingest summary wrong: %v", out)
	}
	outs := out["outcomes"].([]any)
	last := outs[4].(map[string]any)
	if last["reason"] != store.ReasonEmptyMetric {
		t.Fatalf("rejection reason: %v", last)
	}

	// Global stats.
	status, stats := get(t, srv.URL+"/api/v1/stats")
	if status != http.StatusOK {
		t.Fatal(status)
	}
	if stats["samples_accepted"] != float64(4) {
		t.Fatalf("stats: %v", stats)
	}
	if stats["overflow_samples"] != float64(1) {
		t.Fatalf("stats: %v", stats)
	}

	// Metric list.
	status, list := get(t, srv.URL+"/api/v1/metrics")
	if status != http.StatusOK {
		t.Fatal(status)
	}
	metrics := list["metrics"].([]any)
	if len(metrics) != 1 {
		t.Fatalf("metrics list: %v", list)
	}

	// Metric detail without/with series.
	status, detail := get(t, srv.URL+"/api/v1/metrics/http_requests")
	if status != http.StatusOK || detail["count"] != float64(4) {
		t.Fatalf("detail: %d %v", status, detail)
	}
	if _, hasSeries := detail["series"]; hasSeries {
		t.Fatal("series detail should be omitted without ?series=1")
	}
	status, detail = get(t, srv.URL+"/api/v1/metrics/http_requests?series=1")
	if status != http.StatusOK {
		t.Fatal(status)
	}
	series := detail["series"].([]any)
	if len(series) != 4 { // 3 normal + overflow
		t.Fatalf("series entries: %v", series)
	}
	ov := detail["overflow"].(map[string]any)
	if ov["count"] != float64(1) || ov["overflow"] != true {
		t.Fatalf("overflow view: %v", ov)
	}

	// Unknown metric -> 404.
	if status, _ = get(t, srv.URL+"/api/v1/metrics/nope"); status != http.StatusNotFound {
		t.Fatalf("want 404, got %d", status)
	}
}

func TestIngestValidationOverHTTP(t *testing.T) {
	srv, _ := newTestServer(t, nil)
	defer srv.Close()

	cases := []struct {
		name   string
		body   string
		status int
	}{
		{"bad json", `{not json`, http.StatusBadRequest},
		{"empty samples", `{"samples":[]}`, http.StatusBadRequest},
		{"too many samples", `{"samples":[` + strings.Repeat(`{},`, api.MaxSamplesPerRequest) + `{}]}`, http.StatusBadRequest},
		{"wrong method", "", http.StatusMethodNotAllowed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.name == "wrong method" {
				resp, err := http.Get(srv.URL + "/api/v1/ingest")
				if err != nil {
					t.Fatal(err)
				}
				if resp.StatusCode != tc.status {
					t.Fatalf("status %d", resp.StatusCode)
				}
				resp.Body.Close()
				return
			}
			resp, err := http.Post(srv.URL+"/api/v1/ingest", "application/json", strings.NewReader(tc.body))
			if err != nil {
				t.Fatal(err)
			}
			if resp.StatusCode != tc.status {
				t.Fatalf("status %d, want %d", resp.StatusCode, tc.status)
			}
			resp.Body.Close()
		})
	}
}

type recordingFlusher struct {
	mu    sync.Mutex
	count int
	err   error
}

func (r *recordingFlusher) Flush() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.count++
	return r.err
}
func (r *recordingFlusher) calls() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.count
}

func TestFlushEndpoint(t *testing.T) {
	fl := &recordingFlusher{}
	srv, _ := newTestServer(t, fl)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/debug/flush", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK || fl.calls() != 1 {
		t.Fatalf("status %d calls %d", resp.StatusCode, fl.calls())
	}
	resp.Body.Close()
}

func TestFlushDisabled(t *testing.T) {
	srv, _ := newTestServer(t, nil)
	defer srv.Close()
	resp, err := http.Post(srv.URL+"/debug/flush", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("want 501, got %d", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestConcurrentHTTPIngestConservation(t *testing.T) {
	srv, _ := newTestServer(t, nil)
	defer srv.Close()

	const clients, rounds = 12, 100
	var wg sync.WaitGroup
	for c := 0; c < clients; c++ {
		wg.Add(1)
		go func(c int) {
			defer wg.Done()
			for r := 0; r < rounds; r++ {
				status, _ := post(t, srv.URL+"/api/v1/ingest", map[string]any{
					"samples": []map[string]any{
						{"metric": "m", "labels": map[string]string{"stable": "a"}, "value": 1},
						{"metric": "m", "labels": map[string]string{"uid": strings.Repeat("z", 0) + string(rune('a'+c)) + "-" + string(rune('a'+r%26))}, "value": 1},
					},
				})
				if status != http.StatusOK {
					t.Errorf("status %d", status)
					return
				}
			}
		}(c)
	}
	wg.Wait()

	_, stats := get(t, srv.URL+"/api/v1/stats")
	accepted := int64(stats["samples_accepted"].(float64))
	overflow := int64(stats["overflow_samples"].(float64))
	normal := int64(stats["normal_samples"].(float64))
	if accepted != int64(clients*rounds*2) {
		t.Fatalf("accepted %d", accepted)
	}
	if overflow+normal != accepted {
		t.Fatalf("overflow %d + normal %d != accepted %d", overflow, normal, accepted)
	}
}
