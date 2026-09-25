package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"cpathtrace/internal/analyzer"
	"cpathtrace/internal/store"
	"cpathtrace/internal/synthetic"
)

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	st, err := store.NewFileStore(filepath.Join(t.TempDir(), "data"))
	if err != nil {
		t.Fatal(err)
	}
	s := NewServer(st)
	return s, httptest.NewServer(s.Mux)
}

func do(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req, err := http.NewRequest(method, url, &buf)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v (status %d)", err, resp.StatusCode)
	}
	return resp.StatusCode, out
}

func TestIngestAndAnalyze(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	tr, _ := synthetic.Build(synthetic.SerialParallel)
	status, body := do(t, http.MethodPost, ts.URL+"/api/traces", tr)
	if status != http.StatusCreated {
		t.Fatalf("ingest status = %d body=%v", status, body)
	}

	// Fetch raw trace.
	status, body = do(t, http.MethodGet, ts.URL+"/api/traces/"+tr.TraceID, nil)
	if status != http.StatusOK {
		t.Fatalf("get status = %d", status)
	}

	// Analyze: hand-computed 140 (see analyzer tests).
	status, body = do(t, http.MethodGet, ts.URL+"/api/traces/"+tr.TraceID+"/critical", nil)
	if status != http.StatusOK {
		t.Fatalf("critical status = %d body=%v", status, body)
	}
	if body["critical_path_duration"].(float64) != 140 {
		t.Fatalf("critical = %v, want 140", body["critical_path_duration"])
	}

	// List.
	status, body = do(t, http.MethodGet, ts.URL+"/api/traces", nil)
	if status != http.StatusOK {
		t.Fatalf("list status = %d", status)
	}
	ids := body["trace_ids"].([]any)
	if len(ids) != 1 || ids[0].(string) != tr.TraceID {
		t.Fatalf("ids = %v", ids)
	}

	// Delete then 404.
	status, _ = do(t, http.MethodDelete, ts.URL+"/api/traces/"+tr.TraceID, nil)
	if status != http.StatusOK {
		t.Fatalf("delete status = %d", status)
	}
	status, _ = do(t, http.MethodGet, ts.URL+"/api/traces/"+tr.TraceID, nil)
	if status != http.StatusNotFound {
		t.Fatalf("get after delete status = %d, want 404", status)
	}
}

func TestCycleReturns422(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	status, body := do(t, http.MethodPost, ts.URL+"/api/synthetic/seed?name=cycle", nil)
	if status != http.StatusCreated {
		t.Fatalf("seed status = %d body=%v", status, body)
	}
	traceID := body["trace_id"].(string)

	status, body = do(t, http.MethodGet, ts.URL+"/api/traces/"+traceID+"/critical", nil)
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("critical on cycle status = %d, want 422", status)
	}
	diags := body["diagnostics"].([]any)
	codes := map[string]bool{}
	for _, d := range diags {
		codes[d.(map[string]any)["code"].(string)] = true
	}
	if !codes[analyzer.CodeCycle] {
		t.Fatalf("missing CYCLE diagnostic: %v", codes)
	}
	if body["critical_path_duration"] != nil {
		t.Fatalf("critical duration must be null on cycle: %v", body["critical_path_duration"])
	}
}

func TestSeedScenarios(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()
	for _, name := range synthetic.Names() {
		status, body := do(t, http.MethodPost, ts.URL+"/api/synthetic/seed?name="+name, nil)
		if status != http.StatusCreated {
			t.Fatalf("seed %s status=%d body=%v", name, status, body)
		}
	}
	status, body := do(t, http.MethodPost, ts.URL+"/api/synthetic/seed?name=nope", nil)
	if status != http.StatusBadRequest {
		t.Fatalf("bad seed name status = %d", status)
	}
	if body["error"] == nil {
		t.Fatalf("expected error envelope")
	}
}

func TestValidationFailures(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()

	// Unknown field -> strict decoder rejects.
	raw := `{"trace_id":"x","spans":[{"span_id":"a","name":"a","bogus":1}]}`
	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/traces", bytes.NewBufferString(raw))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("unknown field status = %d, want 400", resp.StatusCode)
	}
	resp.Body.Close()

	// Duplicate span id.
	for _, payload := range []string{
		`{"trace_id":"","spans":[]}`,
		`{"trace_id":"x","spans":[]}`,
		`{"trace_id":"x","spans":[
			{"span_id":"a","name":"a","start_time":0,"end_time":1},
			{"span_id":"a","name":"b","start_time":0,"end_time":1}]}`,
		`{"trace_id":"x","spans":[{"span_id":"a","name":"a","relation":"maybe"}]}`,
	} {
		req, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/traces", bytes.NewBufferString(payload))
		req.Header.Set("Content-Type", "application/json")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("payload %q status = %d, want 400", payload, resp.StatusCode)
		}
		resp.Body.Close()
	}
}

func TestHealthz(t *testing.T) {
	_, ts := newTestServer(t)
	defer ts.Close()
	status, body := do(t, http.MethodGet, ts.URL+"/healthz", nil)
	if status != http.StatusOK || body["status"] != "ok" {
		t.Fatalf("health = %d %v", status, body)
	}
}
