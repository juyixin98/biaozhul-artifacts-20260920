package httpapi_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"

	"cardinalgov/internal/governor"
	"cardinalgov/internal/httpapi"
)

func newTestServer(t *testing.T, budget int) (*httptest.Server, *governor.Store) {
	t.Helper()
	cfg := governor.DefaultConfig()
	cfg.DefaultSeriesBudget = budget
	store := governor.NewStore(cfg)
	srv := &httpapi.Server{Store: store}
	ts := httptest.NewServer(srv.NewRouter())
	t.Cleanup(ts.Close)
	return ts, store
}

func postJSON(t *testing.T, ts *httptest.Server, path string, body any) (int, map[string]any) {
	t.Helper()
	buf, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(ts.URL+path, "application/json", bytes.NewReader(buf))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return resp.StatusCode, out
}

func TestIngestSingleAndBatch(t *testing.T) {
	ts, _ := newTestServer(t, 100)

	// 单条对象。
	status, out := postJSON(t, ts, "/ingest", map[string]any{
		"metric": "http_requests",
		"labels": map[string]string{"route": "/login", "pod": "p1"},
		"value":  3,
	})
	if status != http.StatusOK {
		t.Fatalf("single ingest status=%d body=%v", status, out)
	}

	// 批量：含 1 条非法样本 -> 202，其余成功。
	batch := map[string]any{"samples": []map[string]any{
		{"metric": "http_requests", "labels": map[string]string{"pod": "p2"}, "value": 1},
		{"metric": "http_requests", "labels": map[string]string{"pod": "p3"}, "value": 1},
		{"metric": "1-bad", "labels": map[string]string{"pod": "p4"}, "value": 1},
	}}
	status, out = postJSON(t, ts, "/ingest", batch)
	if status != http.StatusAccepted {
		t.Fatalf("partial batch status=%d want 202, body=%v", status, out)
	}
	if out["received"].(float64) != 3 || out["accepted"].(float64) != 2 || out["rejected"].(float64) != 1 {
		t.Fatalf("batch counts wrong: %v", out)
	}
	errs, ok := out["errors"].([]any)
	if !ok || len(errs) != 1 || errs[0].(map[string]any)["reason"] != governor.ReasonInvalidMetricName {
		t.Fatalf("errors wrong: %v", out["errors"])
	}
}

func TestIngestRejectsBadJSON(t *testing.T) {
	ts, _ := newTestServer(t, 10)
	resp, err := http.Post(ts.URL+"/ingest", "application/json", bytes.NewReader([]byte("{not json")))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d want 400", resp.StatusCode)
	}
}

func TestQueryAndStats(t *testing.T) {
	ts, _ := newTestServer(t, 2)

	for i := 0; i < 4; i++ {
		postJSON(t, ts, "/ingest", map[string]any{
			"metric": "http_requests",
			"labels": map[string]string{"uid": fmt.Sprintf("u%d", i)},
			"value":  1,
		})
	}

	resp, err := http.Get(ts.URL + "/metrics/http_requests")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var view governor.MetricView
	if err := json.NewDecoder(resp.Body).Decode(&view); err != nil {
		t.Fatal(err)
	}
	if view.TrackedSeries != 2 || view.Overflow == nil || view.Overflow.SampleCount != 2 {
		t.Fatalf("view wrong: %+v", view)
	}
	if view.SeriesBudget != 2 {
		t.Fatalf("budget in view = %d", view.SeriesBudget)
	}

	resp2, err := http.Get(ts.URL + "/metrics/does_not_exist")
	if err != nil {
		t.Fatal(err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusNotFound {
		t.Fatalf("missing metric status=%d want 404", resp2.StatusCode)
	}

	resp3, err := http.Get(ts.URL + "/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp3.Body.Close()
	var stats map[string]any
	if err := json.NewDecoder(resp3.Body).Decode(&stats); err != nil {
		t.Fatal(err)
	}
	if stats["conservation_ok"] != true {
		t.Fatalf("conservation not ok: %v", stats)
	}
	counters := stats["counters"].(map[string]any)
	if counters["overflowed"].(float64) != 2 {
		t.Fatalf("stats counters wrong: %v", counters)
	}

	resp4, err := http.Get(ts.URL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	defer resp4.Body.Close()
	var names map[string]any
	json.NewDecoder(resp4.Body).Decode(&names)
	if names["count"].(float64) != 1 {
		t.Fatalf("metric list wrong: %v", names)
	}
}

func TestConcurrentHTTPIngest(t *testing.T) {
	ts, store := newTestServer(t, 32)

	const clients = 12
	const perClient = 500
	var wg sync.WaitGroup
	for c := 0; c < clients; c++ {
		wg.Add(1)
		go func(c int) {
			defer wg.Done()
			client := &http.Client{}
			for i := 0; i < perClient; i++ {
				body, _ := json.Marshal(map[string]any{"samples": []map[string]any{
					{"metric": "http_requests", "labels": map[string]string{"uid": fmt.Sprintf("c%d-%d", c, i)}, "value": 1},
					{"metric": "http_requests", "labels": map[string]string{"uid": "stable", "g": fmt.Sprintf("%d", c%2)}, "value": 1},
				}})
				req, _ := http.NewRequest(http.MethodPost, ts.URL+"/ingest", bytes.NewReader(body))
				req.Header.Set("Content-Type", "application/json")
				resp, err := client.Do(req)
				if err != nil {
					t.Errorf("request: %v", err)
					return
				}
				resp.Body.Close()
			}
		}(c)
	}
	wg.Wait()

	ctr := store.Snapshot()
	if err := ctr.CheckConservation(); err != nil {
		t.Fatal(err)
	}
	if ctr.Received != int64(clients*perClient*2) {
		t.Fatalf("received=%d want %d", ctr.Received, clients*perClient*2)
	}
	view, _ := store.QueryMetric("http_requests")
	if view.TrackedSeries > 32 {
		t.Fatalf("tracked %d exceeds budget over HTTP", view.TrackedSeries)
	}
}

func TestSnapshotEndpoint(t *testing.T) {
	cfg := governor.DefaultConfig()
	store := governor.NewStore(cfg)
	path := filepath.Join(t.TempDir(), "snap.json")
	srv := &httpapi.Server{
		Store:        store,
		SaveSnapshot: func() error { return store.SaveSnapshot(path) },
	}
	ts := httptest.NewServer(srv.NewRouter())
	defer ts.Close()

	postJSON(t, ts, "/ingest", map[string]any{"metric": "m", "labels": map[string]string{"a": "b"}, "value": 1})

	resp, err := http.Post(ts.URL+"/admin/snapshot", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("snapshot status=%d", resp.StatusCode)
	}

	restored, err := governor.LoadSnapshot(path)
	if err != nil {
		t.Fatal(err)
	}
	if restored.Snapshot().Received != 1 {
		t.Fatalf("restored counters wrong: %+v", restored.Snapshot())
	}
}

func TestSnapshotEndpointDisabled(t *testing.T) {
	ts, _ := newTestServer(t, 4)
	resp, err := http.Post(ts.URL+"/admin/snapshot", "application/json", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status=%d want 503", resp.StatusCode)
	}
}

func TestHealth(t *testing.T) {
	ts, _ := newTestServer(t, 4)
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health status=%d", resp.StatusCode)
	}
}
