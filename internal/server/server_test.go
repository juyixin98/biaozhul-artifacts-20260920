package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"logmerge/internal/merger"
	"logmerge/internal/store"
)

func newTestServer(t *testing.T) (*Server, *store.Store) {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "entries.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	m := merger.New(merger.Config{
		StartPattern: regexp.MustCompile(`^\d{4}-\d{2}-\d{2}`),
		MaxBytes:     1024,
	}, func(e merger.Entry) {
		if err := st.Append(e); err != nil {
			t.Errorf("append: %v", err)
		}
	})
	return New(m, st), st
}

func TestIngestAndQuery(t *testing.T) {
	srv, _ := newTestServer(t)

	body := `[
		{"source":"a","line":"2026-09-24 10:00:00 ERROR boom"},
		{"source":"a","line":"\tat x.y(Z.java:1)"},
		{"source":"a","line":"2026-09-24 10:00:05 INFO next"}
	]`
	req := httptest.NewRequest(http.MethodPost, "/ingest", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("ingest status %d, body %s", rec.Code, rec.Body)
	}
	var ir ingestResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &ir); err != nil || ir.Accepted != 3 {
		t.Fatalf("ingest response: %v %s", err, rec.Body)
	}

	req = httptest.NewRequest(http.MethodGet, "/logs?source=a", nil)
	rec = httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("logs status %d", rec.Code)
	}
	var lr logsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &lr); err != nil {
		t.Fatal(err)
	}
	if lr.Count != 1 {
		t.Fatalf("expected 1 assembled entry, got %d: %s", lr.Count, rec.Body)
	}
	e := lr.Entries[0]
	if e.LineCount != 2 || !e.Complete || !strings.Contains(e.Message, "Z.java:1") {
		t.Fatalf("assembled entry wrong: %+v", e)
	}
}

func TestIngestSingleObject(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/ingest",
		strings.NewReader(`{"source":"x","line":"2026-09-24 hello"}`))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("status %d: %s", rec.Code, rec.Body)
	}
}

func TestIngestRejectsMissingSource(t *testing.T) {
	srv, _ := newTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/ingest",
		strings.NewReader(`[{"line":"no source"}]`))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
}
