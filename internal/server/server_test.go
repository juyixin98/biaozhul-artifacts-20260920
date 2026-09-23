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

	"cdbg/internal/engine"
	"cdbg/internal/graph"
)

func setupServer(t *testing.T) (http.Handler, string) {
	t.Helper()
	base := t.TempDir()
	work := filepath.Join(base, "work")
	cache := filepath.Join(base, "cache")
	state := filepath.Join(base, "state")
	_ = os.MkdirAll(work, 0o755)
	eng, err := engine.New(cache, state, map[string]string{})
	if err != nil {
		t.Fatal(err)
	}
	return New(eng, work, cache, state).Routes(), work
}

func minimalSpec() *graph.Spec {
	return &graph.Spec{
		Version: "1",
		Tools: map[string]*graph.Tool{
			"write": {Name: "write", ToolVersion: "1", Shell: true,
				Command: []string{"printf '%s' \"$2\" > \"$1\""}},
		},
		Nodes: []*graph.Node{{
			Name: "g", Tool: "write", Outputs: []string{"f.txt"},
			Params: map[string]string{"c": "hi"}, Args: []string{"f.txt", "{{.c}}"},
		}},
	}
}

func postJSON(t *testing.T, h http.Handler, path string, body any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec.Code, out
}

func TestHealth(t *testing.T) {
	h, _ := setupServer(t)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("health code = %d", rec.Code)
	}
}

func TestBuildEndpointCachesSecondRequest(t *testing.T) {
	h, work := setupServer(t)
	body := map[string]any{"spec": minimalSpec()}

	code, out := postJSON(t, h, "/api/v1/build", body)
	if code != http.StatusOK || out["success"] != true {
		t.Fatalf("first build: code=%d body=%v", code, out)
	}
	raw, err := os.ReadFile(filepath.Join(work, "f.txt"))
	if err != nil || string(raw) != "hi" {
		t.Fatalf("output wrong: %q %v", raw, err)
	}

	code, out = postJSON(t, h, "/api/v1/build", body)
	if code != http.StatusOK || out["success"] != true {
		t.Fatalf("second build: code=%d body=%v", code, out)
	}
	nodes := out["nodes"].([]any)
	n0 := nodes[0].(map[string]any)
	if n0["status"] != "cached" {
		t.Fatalf("second request should be cached, got %v", n0["status"])
	}
}

func TestCycleReturns400(t *testing.T) {
	h, _ := setupServer(t)
	spec := minimalSpec()
	// 构造三节点环 a->b->c->a。
	spec.Nodes = []*graph.Node{
		{Name: "a", Tool: "write", Outputs: []string{"a.txt"}, Deps: []string{"c"},
			Params: map[string]string{"c": "a"}, Args: []string{"a.txt", "{{.c}}"}},
		{Name: "b", Tool: "write", Outputs: []string{"b.txt"}, Deps: []string{"a"},
			Params: map[string]string{"c": "b"}, Args: []string{"b.txt", "{{.c}}"}},
		{Name: "c", Tool: "write", Outputs: []string{"c.txt"}, Deps: []string{"b"},
			Params: map[string]string{"c": "c"}, Args: []string{"c.txt", "{{.c}}"}},
	}
	code, out := postJSON(t, h, "/api/v1/build", map[string]any{"spec": spec})
	if code != http.StatusBadRequest {
		t.Fatalf("cycle should be 400, got %d: %v", code, out)
	}
	if !strings.Contains(out["error"].(string), "cycle") {
		t.Fatalf("error should mention cycle: %v", out["error"])
	}
}

func TestExplainIsDryRun(t *testing.T) {
	h, work := setupServer(t)
	code, out := postJSON(t, h, "/api/v1/explain", map[string]any{"spec": minimalSpec()})
	if code != http.StatusOK {
		t.Fatalf("explain code=%d body=%v", code, out)
	}
	if out["dry_run"] != true {
		t.Fatal("explain must be dry_run")
	}
	if _, err := os.Stat(filepath.Join(work, "f.txt")); !os.IsNotExist(err) {
		t.Fatal("explain must not execute commands or create outputs")
	}
}

func TestMissingSpecIs400(t *testing.T) {
	h, _ := setupServer(t)
	code, _ := postJSON(t, h, "/api/v1/build", map[string]any{})
	if code != http.StatusBadRequest {
		t.Fatalf("empty body should be 400, got %d", code)
	}
}

func TestCacheList(t *testing.T) {
	h, _ := setupServer(t)
	if _, out := postJSON(t, h, "/api/v1/build", map[string]any{"spec": minimalSpec()}); out["success"] != true {
		t.Fatalf("build failed: %v", out)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/v1/cache/entries", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("list code=%d", rec.Code)
	}
	var out struct {
		Entries []map[string]any `json:"entries"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if len(out.Entries) != 1 || out.Entries[0]["node"] != "g" {
		t.Fatalf("want 1 entry for node g, got %+v", out.Entries)
	}
}

func TestSpecFileLoading(t *testing.T) {
	h, _ := setupServer(t)
	// 复用 examples 目录之外的临时 spec 文件。
	dir := t.TempDir()
	specPath := filepath.Join(dir, "spec.json")
	raw, _ := json.Marshal(minimalSpec())
	if err := os.WriteFile(specPath, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	code, out := postJSON(t, h, "/api/v1/build", map[string]any{"spec_file": specPath})
	if code != http.StatusOK || out["success"] != true {
		t.Fatalf("spec_file build failed: %d %v", code, out)
	}
}
