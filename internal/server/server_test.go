package server

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"logcluster/internal/engine"
	"logcluster/internal/eval"
)

func newTestSrv() (*Server, http.Handler) {
	eng := engine.New(engine.DefaultConfig(), eval.NewClock())
	s := New(eng, log.New(io.Discard, "", 0))
	return s, s.Routes()
}

func do(t *testing.T, h http.Handler, method, path, ct string, body any) (int, map[string]any) {
	t.Helper()
	var rdr io.Reader
	switch v := body.(type) {
	case string:
		rdr = strings.NewReader(v)
	case []byte:
		rdr = bytes.NewReader(v)
	case nil:
	default:
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, path, rdr)
	if ct != "" {
		req.Header.Set("Content-Type", ct)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	raw, _ := io.ReadAll(rec.Body)
	out := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("response not JSON (%d): %s", rec.Code, raw)
		}
	}
	return rec.Code, out
}

func TestIngestAndQuery(t *testing.T) {
	_, h := newTestSrv()

	status, body := do(t, h, "POST", "/v1/ingest", "application/json", map[string]any{
		"lines": []string{
			"user 5 logged in",
			"user 9 logged in",
			"connection refused to host",
			"connection timeout to host",
		},
	})
	if status != 200 {
		t.Fatalf("ingest status %d: %v", status, body)
	}
	if body["ingested"].(float64) != 4 {
		t.Fatalf("ingested = %v", body["ingested"])
	}
	results := body["results"].([]any)
	cidNum := results[0].(map[string]any)["cluster_id"]
	cidWord := results[1].(map[string]any)["cluster_id"]
	if cidNum != cidWord {
		t.Error("number variants did not cluster")
	}
	refused := results[2].(map[string]any)["cluster_id"]
	timedOut := results[3].(map[string]any)["cluster_id"]
	if refused == timedOut {
		t.Error("refused/timeout were merged")
	}

	status, body = do(t, h, "GET", "/v1/templates", "", nil)
	if status != 200 || body["count"].(float64) != 3 {
		t.Fatalf("templates: %d %v", status, body["count"])
	}

	status, body = do(t, h, "GET", "/v1/events?q=refused", "", nil)
	if status != 200 || body["count"].(float64) != 1 {
		t.Fatalf("events filter: %d %v", status, body["count"])
	}

	status, _ = do(t, h, "GET", "/v1/templates/9999", "", nil)
	if status != http.StatusNotFound {
		t.Fatalf("missing template status = %d, want 404", status)
	}

	status, body = do(t, h, "GET", "/v1/metrics", "", nil)
	if status != 200 || body["total_lines"].(float64) != 4 {
		t.Fatalf("metrics: %d %v", status, body)
	}
}

func TestIngestTextPlain(t *testing.T) {
	_, h := newTestSrv()
	status, body := do(t, h, "POST", "/v1/ingest", "text/plain",
		"line one 5\nline two 7\n\n  \nline one 9")
	if status != 200 {
		t.Fatalf("status %d: %v", status, body)
	}
	if body["ingested"].(float64) != 3 {
		t.Fatalf("ingested = %v", body["ingested"])
	}
}

func TestBadBodies(t *testing.T) {
	_, h := newTestSrv()
	status, _ := do(t, h, "POST", "/v1/ingest", "application/json", "{not json")
	if status != http.StatusBadRequest {
		t.Errorf("bad json status = %d", status)
	}
	status, _ = do(t, h, "POST", "/v1/ingest", "application/json", map[string]any{})
	if status != http.StatusBadRequest {
		t.Errorf("empty ingest status = %d", status)
	}
	status, _ = do(t, h, "POST", "/v1/ingest", "application/xml", "<x/>")
	if status != http.StatusUnsupportedMediaType {
		t.Errorf("ct status = %d", status)
	}
	status, _ = do(t, h, "GET", "/v1/events?cluster_id=abc", "", nil)
	if status != http.StatusBadRequest {
		t.Errorf("bad cluster_id status = %d", status)
	}
}

func TestSnapshotEndpoint(t *testing.T) {
	_, h := newTestSrv()
	do(t, h, "POST", "/v1/ingest", "application/json", map[string]any{"line": "hello world 5"})

	path := t.TempDir() + "/snap.json"
	status, body := do(t, h, "POST", "/v1/snapshot", "application/json", map[string]any{"path": path})
	if status != 200 {
		t.Fatalf("snapshot: %d %v", status, body)
	}

	// Load into a fresh engine through a second server instance.
	eng2 := engine.New(engine.DefaultConfig(), eval.NewClock())
	loaded, err := eng2.LoadIfExists(path)
	if err != nil || !loaded {
		t.Fatalf("reload: %v %v", loaded, err)
	}
	s2 := New(eng2, log.New(io.Discard, "", 0))
	status, body = do(t, s2.Routes(), "GET", "/v1/templates", "", nil)
	if status != 200 || body["count"].(float64) != 1 {
		t.Fatalf("templates after reload: %d %v", status, body)
	}
}

func TestHealth(t *testing.T) {
	_, h := newTestSrv()
	status, body := do(t, h, "GET", "/healthz", "", nil)
	if status != 200 || body["status"] != "ok" {
		t.Fatalf("health: %d %v", status, body)
	}
}
