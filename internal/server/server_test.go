package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func newTestServer(t *testing.T) (*Server, string) {
	t.Helper()
	cache := t.TempDir()
	s, err := New(Config{CacheDir: cache})
	if err != nil {
		t.Fatal(err)
	}
	return s, cache
}

func doApply(t *testing.T, h http.Handler, body string) (int, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/apply", strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("bad json %q: %v", rec.Body.String(), err)
		}
	}
	return rec.Code, out
}

func TestHealth(t *testing.T) {
	s, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d", rec.Code)
	}
}

func TestApplyEndpointSuccess(t *testing.T) {
	s, _ := newTestServer(t)
	work := t.TempDir()
	if err := os.WriteFile(filepath.Join(work, "f.txt"), []byte("a\nb\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	body := map[string]any{
		"workdir": work,
		"patches": []map[string]any{{
			"diff": "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n",
		}},
	}
	raw, _ := json.Marshal(body)
	code, out := doApply(t, s.Handler(), string(raw))
	if code != http.StatusOK {
		t.Fatalf("status %d body %v", code, out)
	}
	if out["status"] != "applied" {
		t.Fatalf("status = %v", out["status"])
	}
	got, _ := os.ReadFile(filepath.Join(work, "f.txt"))
	if string(got) != "a\nB\n" {
		t.Fatalf("got %q", got)
	}
}

func TestApplyEndpointDryRun(t *testing.T) {
	s, _ := newTestServer(t)
	work := t.TempDir()
	if err := os.WriteFile(filepath.Join(work, "f.txt"), []byte("a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	body := `{"workdir":"` + work + `","dry_run":true,"patches":[{"diff":"--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+A\n"}]}`
	code, out := doApply(t, s.Handler(), body)
	if code != http.StatusOK {
		t.Fatalf("status %d %v", code, out)
	}
	if out["status"] != "validated" {
		t.Fatalf("status = %v", out["status"])
	}
	got, _ := os.ReadFile(filepath.Join(work, "f.txt"))
	if string(got) != "a\n" {
		t.Fatalf("dry run modified the file: %q", got)
	}
}

func TestApplyEndpointBadContext(t *testing.T) {
	s, _ := newTestServer(t)
	work := t.TempDir()
	if err := os.WriteFile(filepath.Join(work, "f.txt"), []byte("a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(filepath.Join(work, "f.txt"))
	body := `{"workdir":"` + work + `","patches":[{"diff":"--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-WRONG\n+A\n"}]}`
	code, out := doApply(t, s.Handler(), body)
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("status %d", code)
	}
	errObj, _ := out["error"].(map[string]any)
	if errObj["code"] != "context_mismatch" {
		t.Fatalf("code = %v", errObj["code"])
	}
	after, _ := os.ReadFile(filepath.Join(work, "f.txt"))
	if !bytes.Equal(before, after) {
		t.Fatal("workdir changed after rejected request")
	}
}

func TestApplyEndpointRejectsInvalidJSON(t *testing.T) {
	s, _ := newTestServer(t)
	code, _ := doApply(t, s.Handler(), "{not json")
	if code != http.StatusBadRequest {
		t.Fatalf("status %d", code)
	}
}

func TestApplyEndpointRejectsMissingWorkdir(t *testing.T) {
	s, _ := newTestServer(t)
	code, _ := doApply(t, s.Handler(), `{"patches":[]}`)
	if code != http.StatusBadRequest {
		t.Fatalf("status %d", code)
	}
}

func TestApplyEndpointCreateAndDelete(t *testing.T) {
	s, _ := newTestServer(t)
	work := t.TempDir()
	if err := os.WriteFile(filepath.Join(work, "gone.txt"), []byte("x\ny\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	body := map[string]any{
		"workdir": work,
		"patches": []map[string]any{
			{"diff": "--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1,1 @@\n+hi\n"},
			{"diff": "--- a/gone.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"},
		},
	}
	raw, _ := json.Marshal(body)
	code, out := doApply(t, s.Handler(), string(raw))
	if code != http.StatusOK {
		t.Fatalf("status %d %v", code, out)
	}
	if _, err := os.Stat(filepath.Join(work, "created.txt")); err != nil {
		t.Fatalf("created file missing: %v", err)
	}
	if _, err := os.Stat(filepath.Join(work, "gone.txt")); !os.IsNotExist(err) {
		t.Fatal("deleted file still present")
	}
}
