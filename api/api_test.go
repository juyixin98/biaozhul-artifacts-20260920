package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"counterreset/store"
)

func newTestServer(t *testing.T) (http.Handler, *store.Store) {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return New(st), st
}

func do(t *testing.T, h http.Handler, method, path, body string) (int, map[string]any) {
	t.Helper()
	var r *http.Request
	if body != "" {
		r = httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
	} else {
		r = httptest.NewRequest(method, path, nil)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	var out map[string]any
	if w.Body.Len() > 0 {
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-JSON response %d: %s", w.Code, w.Body.String())
		}
	}
	return w.Code, out
}

func TestHealth(t *testing.T) {
	h, _ := newTestServer(t)
	code, body := do(t, h, "GET", "/healthz", "")
	if code != 200 || body["status"] != "ok" {
		t.Fatalf("health = %d %v", code, body)
	}
}

func TestIngestAndQuery(t *testing.T) {
	h, _ := newTestServer(t)
	body := `{"metric":"requests_total","labels":{"host":"a"},
"samples":[{"t":0,"value":0},{"t":10,"value":10},{"t":20,"value":3}]}`
	code, resp := do(t, h, "POST", "/api/v1/series", body)
	if code != 200 {
		t.Fatalf("ingest code=%d body=%v", code, resp)
	}

	code, q := do(t, h, "GET",
		"/api/v1/query?metric=requests_total&label.host=a&from=0&to=20&max_interval=1000", "")
	if code != 200 {
		t.Fatalf("query code=%d", code)
	}
	results := q["result"].([]any)
	if len(results) != 1 {
		t.Fatalf("want 1 result, got %d", len(results))
	}
	r0 := results[0].(map[string]any)
	// Reset pair contributes post-reset value 3; first pair +10 -> 13.
	if inc := r0["increase"].(float64); inc != 13 {
		t.Errorf("increase = %v, want 13", inc)
	}
	resets := r0["resets"].([]any)
	if len(resets) != 1 {
		t.Errorf("want 1 reset, got %d", len(resets))
	}
}

func TestNegativeValueHTTPRejected(t *testing.T) {
	h, _ := newTestServer(t)
	body := `{"metric":"m","samples":[{"t":1,"value":-5}]}`
	code, resp := do(t, h, "POST", "/api/v1/series", body)
	if code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", code)
	}
	if !strings.Contains(resp["error"].(string), "negative") {
		t.Errorf("error should mention negative, got %v", resp["error"])
	}
}

func TestDuplicateTimestampConflict(t *testing.T) {
	h, _ := newTestServer(t)
	_, code := ingestRaw(t, h, `{"metric":"m","samples":[{"t":1,"value":1}]}`)
	if code != 200 {
		t.Fatalf("first ingest code=%d", code)
	}
	_, code = ingestRaw(t, h, `{"metric":"m","samples":[{"t":1,"value":2}]}`)
	if code != http.StatusConflict {
		t.Fatalf("clashing duplicate want 409, got %d", code)
	}
	// Same value: idempotent 200.
	_, code = ingestRaw(t, h, `{"metric":"m","samples":[{"t":1,"value":1}]}`)
	if code != 200 {
		t.Fatalf("same-value duplicate want 200, got %d", code)
	}
}

func TestQueryMissingMetric(t *testing.T) {
	h, _ := newTestServer(t)
	code, _ := do(t, h, "GET", "/api/v1/query?from=0&to=10", "")
	if code != 400 {
		t.Fatalf("want 400 without metric, got %d", code)
	}
}

func TestQueryRFC3339(t *testing.T) {
	h, _ := newTestServer(t)
	// Ingest using Unix seconds...
	_, code := ingestRaw(t, h, `{"metric":"m","samples":[{"t":1700000000,"value":0},{"t":1700000060,"value":30}]}`)
	if code != 200 {
		t.Fatalf("ingest %d", code)
	}
	// ...query with RFC3339 strings.
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET",
		"/api/v1/query?metric=m&from=2023-11-14T22:13:20Z&to=2023-11-14T22:13:40Z&max_interval=1000", nil)
	h.ServeHTTP(w, r)
	var out map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	inc := out["result"].([]any)[0].(map[string]any)["increase"].(float64)
	// Window starts exactly at the first sample and is 20s long: 1/3 of +30 = 10.
	if inc != 10 {
		t.Errorf("rfc3339 boundary interpolation = %v, want 10", inc)
	}
}

func ingestRaw(t *testing.T, h http.Handler, body string) (map[string]any, int) {
	t.Helper()
	code, resp := do(t, h, "POST", "/api/v1/series", body)
	return resp, code
}
