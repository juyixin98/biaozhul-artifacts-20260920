package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"metricrollup/internal/store"
)

func newTestServer(t *testing.T) (*Server, *store.Store) {
	t.Helper()
	st := store.New()
	return New(st, "", nil), st
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	var parsed map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &parsed); err != nil {
			t.Fatalf("response not JSON (%d): %s", rec.Code, rec.Body.String())
		}
	}
	return rec, parsed
}

func TestHealth(t *testing.T) {
	s, _ := newTestServer(t)
	rec, body := doJSON(t, s.Handler(), http.MethodGet, "/healthz", nil)
	if rec.Code != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health = %d %v", rec.Code, body)
	}
}

func TestIngestThenQuery(t *testing.T) {
	s, _ := newTestServer(t)
	rec, body := doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "cpu", "labels": map[string]string{"host": "a"}, "ts": 7200, "value": 10},
			{"metric": "cpu", "labels": map[string]string{"host": "a"}, "ts": 7250, "value": 20},
		},
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("ingest = %d %v", rec.Code, body)
	}
	if body["ingested"] != float64(2) {
		t.Fatalf("ingested count = %v", body["ingested"])
	}

	rec, body = doJSON(t, s.Handler(), http.MethodGet,
		"/v1/query?metric=cpu&label=host%3Da&layer=minute&start=7200&end=7260", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("query = %d %v", rec.Code, body)
	}
	buckets := body["buckets"].([]any)
	if len(buckets) != 1 {
		t.Fatalf("want 1 bucket, got %d", len(buckets))
	}
	b0 := buckets[0].(map[string]any)
	if b0["count"] != float64(2) || b0["sum"] != float64(30) {
		t.Fatalf("bucket wrong: %v", b0)
	}
}

func TestIngestValidation(t *testing.T) {
	s, _ := newTestServer(t)

	cases := []struct {
		name string
		body map[string]any
	}{
		{"empty samples", map[string]any{"samples": []any{}}},
		{"missing metric", map[string]any{"samples": []any{map[string]any{"ts": 1, "value": 1}}}},
		{"negative ts", map[string]any{"samples": []any{map[string]any{"metric": "m", "ts": -1, "value": 1}}}},
		{"nan value", map[string]any{"samples": []any{map[string]any{"metric": "m", "ts": 1, "value": "NaN"}}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// NaN can't be expressed in JSON; send raw text for that case.
			if tc.name == "nan value" {
				req := httptest.NewRequest(http.MethodPost, "/v1/ingest",
					strings.NewReader(`{"samples":[{"metric":"m","ts":1,"value":NaN}]}`))
				rec := httptest.NewRecorder()
				s.Handler().ServeHTTP(rec, req)
				if rec.Code != http.StatusBadRequest {
					t.Fatalf("want 400, got %d: %s", rec.Code, rec.Body.String())
				}
				return
			}
			rec, body := doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", tc.body)
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("want 400, got %d %v", rec.Code, body)
			}
		})
	}
}

func TestUnknownFieldRejected(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/ingest",
		strings.NewReader(`{"samples":[{"metric":"m","ts":1,"value":1}],"bogus":true}`))
	rec := httptest.NewRecorder()
	s, _ := newTestServer(t)
	s.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for unknown field, got %d", rec.Code)
	}
}

func TestQueryMissingSeries(t *testing.T) {
	s, _ := newTestServer(t)
	rec, _ := doJSON(t, s.Handler(), http.MethodGet,
		"/v1/query?metric=nope&layer=hour&start=0&end=3600", nil)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestQueryBadLayer(t *testing.T) {
	s, _ := newTestServer(t)
	rec, _ := doJSON(t, s.Handler(), http.MethodGet,
		"/v1/query?metric=m&layer=day&start=0&end=60", nil)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

func TestCorrectLifecycle(t *testing.T) {
	s, _ := newTestServer(t)
	doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "cpu", "labels": map[string]string{"host": "a"}, "ts": 0, "value": 1},
			{"metric": "cpu", "labels": map[string]string{"host": "a"}, "ts": 30, "value": 3},
		},
	})

	// Correct an existing second.
	rec, body := doJSON(t, s.Handler(), http.MethodPost, "/v1/correct", map[string]any{
		"metric": "cpu", "labels": map[string]string{"host": "a"}, "ts": 0, "value": 100,
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("correct = %d %v", rec.Code, body)
	}

	rec, _ = doJSON(t, s.Handler(), http.MethodGet,
		"/v1/query?metric=cpu&label=host%3Da&layer=minute&start=0&end=60", nil)
	var q map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &q)
	b0 := q["buckets"].([]any)[0].(map[string]any)
	if b0["count"] != float64(2) || b0["sum"] != float64(103) {
		t.Fatalf("propagated minute wrong: %v", b0)
	}

	// Correcting a second that was never ingested -> 409.
	rec, _ = doJSON(t, s.Handler(), http.MethodPost, "/v1/correct", map[string]any{
		"metric": "cpu", "labels": map[string]string{"host": "a"}, "ts": 45, "value": 1,
	})
	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409 for unrecoverable correction, got %d", rec.Code)
	}
}

func TestPruneEndpoint(t *testing.T) {
	s, _ := newTestServer(t)
	doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "cpu", "ts": 0, "value": 1},
			{"metric": "cpu", "ts": 4000, "value": 2},
		},
	})
	rec, body := doJSON(t, s.Handler(), http.MethodPost, "/v1/admin/prune", map[string]any{"cutoff": 3600})
	if rec.Code != http.StatusOK || body["removed_raw_samples"] != float64(1) {
		t.Fatalf("prune = %d %v", rec.Code, body)
	}

	// Raw layer at 0 is now empty; minute/hour still populated.
	rec, _ = doJSON(t, s.Handler(), http.MethodGet, "/v1/query?metric=cpu&layer=raw&start=0&end=1", nil)
	var q map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &q)
	if q["buckets"].([]any)[0].(map[string]any)["count"] != float64(0) {
		t.Fatal("pruned raw bucket should be empty")
	}
}

func TestSnapshotDisabledByDefault(t *testing.T) {
	s, _ := newTestServer(t)
	rec, _ := doJSON(t, s.Handler(), http.MethodPost, "/v1/admin/snapshot", nil)
	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409 when persistence disabled, got %d", rec.Code)
	}
}

func TestSeriesList(t *testing.T) {
	s, _ := newTestServer(t)
	doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "m", "labels": map[string]string{"k": "v"}, "ts": 0, "value": 1},
		},
	})
	rec, body := doJSON(t, s.Handler(), http.MethodGet, "/v1/series", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("series = %d", rec.Code)
	}
	if len(body["series"].([]any)) != 1 {
		t.Fatalf("want 1 series, got %v", body["series"])
	}
}

func TestQueryAcceptsRFC3339(t *testing.T) {
	s, _ := newTestServer(t)
	doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "m", "ts": 0, "value": 1},
		},
	})
	// 1970-01-01T00:00:00Z .. 1970-01-01T00:01:00Z
	rec, body := doJSON(t, s.Handler(), http.MethodGet,
		"/v1/query?metric=m&layer=minute&start=1970-01-01T00:00:00Z&end=1970-01-01T00:01:00Z", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("RFC3339 query = %d %v", rec.Code, body)
	}
	if len(body["buckets"].([]any)) != 1 {
		t.Fatalf("want 1 bucket, got %v", body["buckets"])
	}
}

func TestQueryBadParams(t *testing.T) {
	s, _ := newTestServer(t)
	cases := []string{
		"/v1/query?layer=minute&start=0&end=60",                 // missing metric
		"/v1/query?metric=m&layer=minute&end=60",                // missing start
		"/v1/query?metric=m&layer=minute&start=0",               // missing end
		"/v1/query?metric=m&layer=minute&start=x&end=60",        // unparseable start
		"/v1/query?metric=m&layer=minute&start=0&end=y",         // unparseable end
		"/v1/query?metric=m&layer=minute&start=60&end=60",       // empty range
		"/v1/query?metric=m&layer=minute&start=0&end=60&label=", // malformed label
	}
	for _, path := range cases {
		rec, _ := doJSON(t, s.Handler(), http.MethodGet, path, nil)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: want 400, got %d", path, rec.Code)
		}
	}
}

func TestCorrectRejectsOversizeNumber(t *testing.T) {
	s, _ := newTestServer(t)
	// 1e999 overflows float64: JSON decoding must reject it with 400.
	req := httptest.NewRequest(http.MethodPost, "/v1/correct",
		strings.NewReader(`{"metric":"m","ts":0,"value":1e999}`))
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for oversize number, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestCorrectMissingMetric(t *testing.T) {
	s, _ := newTestServer(t)
	rec, _ := doJSON(t, s.Handler(), http.MethodPost, "/v1/correct", map[string]any{
		"ts": 0, "value": 1,
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

func TestPruneValidation(t *testing.T) {
	s, _ := newTestServer(t)
	rec, _ := doJSON(t, s.Handler(), http.MethodPost, "/v1/admin/prune", map[string]any{"cutoff": 0})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}

func TestSnapshotSaveRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "snap.json")
	st := store.New()
	s := New(st, path, nil)
	doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{{"metric": "m", "ts": 0, "value": 7}},
	})
	rec, body := doJSON(t, s.Handler(), http.MethodPost, "/v1/admin/snapshot", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("snapshot = %d %v", rec.Code, body)
	}

	restored := store.New()
	if err := restored.Load(path); err != nil {
		t.Fatal(err)
	}
	buckets, err := restored.Query("m", nil, 0, 1, store.LayerRaw)
	if err != nil {
		t.Fatal(err)
	}
	if buckets[0].Count != 1 || *buckets[0].Sum != 7 {
		t.Fatalf("snapshot file content wrong: %+v", buckets[0])
	}
}

func TestIngestRejectsEmptyLabelKey(t *testing.T) {
	s, _ := newTestServer(t)
	rec, _ := doJSON(t, s.Handler(), http.MethodPost, "/v1/ingest", map[string]any{
		"samples": []map[string]any{
			{"metric": "m", "labels": map[string]string{" ": "v"}, "ts": 0, "value": 1},
		},
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for blank label key, got %d", rec.Code)
	}
}

func TestMalformedBody(t *testing.T) {
	s, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/ingest", strings.NewReader("{not json"))
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
}
