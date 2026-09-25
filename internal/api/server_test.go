package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"licensejudge/internal/policy"
	"licensejudge/internal/runner"
)

func newTestServer(t *testing.T) http.Handler {
	t.Helper()
	pol := &policy.Policy{
		Name:     "test",
		Licenses: map[string]string{"MIT": "allow", "GPL-3.0-only": "deny"},
	}
	base := t.TempDir()
	rn, err := runner.NewRunner(&runner.Manifest{}, base,
		filepath.Join(base, "work"), filepath.Join(base, "cache"), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return (&Server{Policy: pol, Runner: rn}).NewMux()
}

func postJSON(t *testing.T, h http.Handler, path string, body any) (int, map[string]any) {
	t.Helper()
	buf, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(buf))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &out)
	}
	return rec.Code, out
}

func get(t *testing.T, h http.Handler, path string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestJudgeEndpoint(t *testing.T) {
	h := newTestServer(t)

	if code, body := postJSON(t, h, "/v1/judge", map[string]string{"expression": "MIT OR GPL-3.0-only"}); code != http.StatusOK {
		t.Fatalf("status=%d body=%v", code, body)
	} else if body["decision"] != "allow" || body["selection"] != "MIT" {
		t.Errorf("unexpected body: %v", body)
	}

	if code, body := postJSON(t, h, "/v1/judge", map[string]string{"expression": "GPL-3.0-only"}); code != http.StatusOK {
		t.Fatalf("status=%d", code)
	} else if body["decision"] != "deny" {
		t.Errorf("deny case: %v", body)
	}

	if code, body := postJSON(t, h, "/v1/judge", map[string]string{"expression": "Mystery-1.0"}); code != http.StatusOK {
		t.Fatalf("status=%d", code)
	} else if body["decision"] != "unknown" {
		t.Errorf("unknown must stay unknown, got %v", body)
	}

	if code, _ := postJSON(t, h, "/v1/judge", map[string]string{"expression": "MIT OR"}); code != http.StatusBadRequest {
		t.Errorf("invalid expression should be 400, got %d", code)
	}
	if code, _ := postJSON(t, h, "/v1/judge", map[string]string{}); code != http.StatusBadRequest {
		t.Errorf("missing expression should be 400, got %d", code)
	}
}

func TestHealthAndPolicy(t *testing.T) {
	h := newTestServer(t)
	if code, body := get(t, h, "/healthz"); code != http.StatusOK || body["status"] != "ok" {
		t.Errorf("health: %d %v", code, body)
	}
	if code, body := get(t, h, "/v1/policy"); code != http.StatusOK || body["name"] != "test" {
		t.Errorf("policy: %d %v", code, body)
	}
}

func TestRunFixtureRejectsUnregistered(t *testing.T) {
	h := newTestServer(t)
	code, body := postJSON(t, h, "/v1/fixtures/run", map[string]string{"name": "anything"})
	if code != http.StatusBadRequest {
		t.Fatalf("unregistered fixture must be rejected, got %d %v", code, body)
	}
}
