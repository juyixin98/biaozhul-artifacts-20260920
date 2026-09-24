package httpapi_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"smtprecv/internal/httpapi"
	"smtprecv/internal/store"
)

func setup(t *testing.T) (http.Handler, *store.Store) {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "mail"))
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	return httpapi.NewHandler(st), st
}

func TestHealth(t *testing.T) {
	h, _ := setup(t)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"ok"`) {
		t.Errorf("body = %s", rec.Body.String())
	}
}

func TestListGetRawDelete(t *testing.T) {
	h, st := setup(t)
	m, err := st.Add("alice@x", []string{"bob@y", "carol@y"}, "Subject: hi\r\n\r\nbody\r\nline2")
	if err != nil {
		t.Fatal(err)
	}

	// List
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/messages", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("list code = %d: %s", rec.Code, rec.Body.String())
	}
	var list struct {
		Count    int              `json:"count"`
		Messages []*store.Message `json:"messages"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	if list.Count != 1 || list.Messages[0].ID != m.ID {
		t.Fatalf("list = %+v", list)
	}
	if list.Messages[0].Data != "" {
		t.Errorf("listing leaked body data: %q", list.Messages[0].Data)
	}
	if len(list.Messages[0].To) != 2 {
		t.Errorf("to = %v", list.Messages[0].To)
	}

	// Get by ID
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/messages/"+m.ID, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("get code = %d", rec.Code)
	}
	var got store.Message
	json.Unmarshal(rec.Body.Bytes(), &got)
	if got.Data != "Subject: hi\r\n\r\nbody\r\nline2" {
		t.Errorf("data = %q", got.Data)
	}

	// Raw
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/messages/"+m.ID+"/raw", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("raw code = %d", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "message/rfc822" {
		t.Errorf("content-type = %q", ct)
	}
	if rec.Body.String() != got.Data {
		t.Errorf("raw body = %q", rec.Body.String())
	}

	// Delete
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodDelete, "/messages/"+m.ID, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("delete code = %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/messages/"+m.ID, nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404 after delete, got %d", rec.Code)
	}
}

func TestErrorsAndMethods(t *testing.T) {
	h, _ := setup(t)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/messages/nope", nil))
	if rec.Code != http.StatusNotFound {
		t.Errorf("missing id: code = %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/messages/../etc/passwd", nil)
	h.ServeHTTP(rec, req)
	// ServeMux cleans the path (301) or the cleaned path misses the handler;
	// in neither case must it 200 through to file access.
	if rec.Code == http.StatusOK {
		t.Errorf("traversal: code = %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/messages", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("post messages: code = %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/messages?limit=abc", nil))
	if rec.Code != http.StatusBadRequest {
		t.Errorf("bad limit: code = %d", rec.Code)
	}
}

func TestRequireLoopback(t *testing.T) {
	if err := httpapi.RequireLoopback("0.0.0.0:8080"); err == nil {
		t.Fatal("expected non-loopback rejection")
	}
	if err := httpapi.RequireLoopback("127.0.0.1:8080"); err != nil {
		t.Errorf("loopback rejected: %v", err)
	}
	if err := httpapi.RequireLoopback("[::1]:8080"); err != nil {
		t.Errorf("ipv6 loopback rejected: %v", err)
	}
}
