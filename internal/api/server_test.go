package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"depresolve/internal/cache"
)

const diamondRequest = `{
  "registry": {
    "a": [{"version": "1.0.0", "deps": {"b": "^1.4", "c": "^1.2"}}],
    "b": [
      {"version": "1.5.0", "deps": {"d": "^2.0.0"}},
      {"version": "1.4.0", "deps": {"d": "^1.0.0"}}
    ],
    "c": [{"version": "1.2.0", "deps": {"d": "^1.0.0"}}],
    "d": [{"version": "2.0.0"}, {"version": "1.2.0"}, {"version": "1.0.0"}]
  },
  "root": {"a": "^1.0.0"}
}`

func post(t *testing.T, h http.Handler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/solve", bytes.NewBufferString(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestSolveEndpoint(t *testing.T) {
	srv := NewServer(nil)
	rec := post(t, srv.Handler(), diamondRequest)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	var resp SolveResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.OK {
		t.Fatalf("expected solution, got %+v", resp.Conflict)
	}
	want := map[string]string{"a": "1.0.0", "b": "1.4.0", "c": "1.2.0", "d": "1.2.0"}
	for k, v := range want {
		if resp.Solution[k] != v {
			t.Errorf("solution[%s] = %q, want %q", k, resp.Solution[k], v)
		}
	}
	if len(resp.Log) == 0 {
		t.Error("expected a decision log")
	}
}

func TestSolveEndpointConflict(t *testing.T) {
	srv := NewServer(nil)
	body := `{
	  "registry": {
	    "a": [{"version": "1.0.0", "deps": {"b": "^2.0.0"}}],
	    "b": [{"version": "1.0.0"}]
	  },
	  "root": {"a": "^1.0.0"}
	}`
	rec := post(t, srv.Handler(), body)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
	var resp SolveResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.OK || resp.Conflict == nil {
		t.Fatalf("expected conflict, got %+v", resp)
	}
	if resp.Conflict.Package != "b" {
		t.Errorf("conflict package = %q, want b", resp.Conflict.Package)
	}
}

func TestSolveEndpointBadRequests(t *testing.T) {
	srv := NewServer(nil)
	for name, body := range map[string]string{
		"malformed":     `{not json`,
		"empty":         `{}`,
		"no root":       `{"registry": {"a": [{"version": "1.0.0"}]}}`,
		"unknown field": `{"registry": {}, "root": {}, "bogus": 1}`,
		"bad version":   `{"registry": {"a": [{"version": "1.x"}]}, "root": {"a": "*"}}`,
		"unknown dep":   `{"registry": {"a": [{"version": "1.0.0", "deps": {"ghost": "*"}}]}, "root": {"a": "*"}}`,
	} {
		rec := post(t, srv.Handler(), body)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: status %d, want 400 (%s)", name, rec.Code, rec.Body)
		}
	}
}

func TestHealthz(t *testing.T) {
	srv := NewServer(nil)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestCacheHit(t *testing.T) {
	dir := t.TempDir()
	c, err := cache.Open(filepath.Join(dir, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	srv := NewServer(c)

	rec1 := post(t, srv.Handler(), diamondRequest)
	var r1 SolveResponse
	if err := json.Unmarshal(rec1.Body.Bytes(), &r1); err != nil {
		t.Fatal(err)
	}
	if r1.Cached {
		t.Error("first request must not be cached")
	}

	rec2 := post(t, srv.Handler(), diamondRequest)
	var r2 SolveResponse
	if err := json.Unmarshal(rec2.Body.Bytes(), &r2); err != nil {
		t.Fatal(err)
	}
	if !r2.Cached {
		t.Error("second identical request should be served from cache")
	}
	if !r2.OK || r2.Solution["b"] != "1.4.0" {
		t.Errorf("cached response wrong: %+v", r2.Solution)
	}

	// disable_cache bypasses the cache.
	req := httptest.NewRequest(http.MethodPost, "/v1/solve",
		bytes.NewBufferString(`{"registry": {"x": [{"version": "1.0.0"}]}, "root": {"x": "*"}, "options": {"disable_cache": true}}`))
	rec3 := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec3, req)
	var r3 SolveResponse
	if err := json.Unmarshal(rec3.Body.Bytes(), &r3); err != nil {
		t.Fatal(err)
	}
	if r3.Cached {
		t.Error("disable_cache must bypass the cache")
	}
}

func TestCacheDirOutsideWorkdir(t *testing.T) {
	dir, err := cache.DefaultDir()
	if err != nil {
		t.Fatal(err)
	}
	abs, err := filepath.Abs(".")
	if err != nil {
		t.Fatal(err)
	}
	rel, err := filepath.Rel(abs, dir)
	if err != nil {
		t.Fatal(err)
	}
	inside := rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)))
	if inside {
		t.Errorf("cache dir %q must not be inside working dir %q", dir, abs)
	}
}
