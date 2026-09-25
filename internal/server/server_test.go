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

	"cdag/internal/cache"
)

func setupServer(t *testing.T) (http.Handler, string) {
	t.Helper()
	root := t.TempDir()
	work := filepath.Join(root, "ws")
	if err := os.MkdirAll(work, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(work, "src.txt"), []byte("hi\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store, err := cache.Open(filepath.Join(root, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	return New(store).Routes(), work
}

func doJSON(t *testing.T, h http.Handler, method, path string, body any) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	if rec.Body.Len() > 0 {
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("non-JSON response %d: %s", rec.Code, rec.Body.String())
		}
	}
	return rec, out
}

func validProject(workdir string) map[string]any {
	return map[string]any{
		"id":      "p1",
		"workdir": workdir,
		"graph": map[string]any{
			"nodes": []any{
				map[string]any{"id": "src", "outputs": []string{"src.txt"}},
				map[string]any{
					"id": "up", "command": "tr a-z A-Z < src.txt > up.txt",
					"inputs": []string{"src.txt"}, "outputs": []string{"up.txt"},
					"depends_on": []string{"src"},
				},
			},
		},
	}
}

func TestHealth(t *testing.T) {
	h, _ := setupServer(t)
	rec, out := doJSON(t, h, "GET", "/healthz", nil)
	if rec.Code != http.StatusOK || out["status"] != "ok" {
		t.Fatalf("health: %d %v", rec.Code, out)
	}
}

func TestRegisterValidateBuildFlow(t *testing.T) {
	h, work := setupServer(t)

	// Invalid JSON -> 400.
	rec, _ := doJSON(t, h, "POST", "/v1/projects", map[string]any{"id": "x"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("missing workdir should be 400, got %d: %s", rec.Code, rec.Body.String())
	}

	// Cycle -> 400 with cycle code.
	cycProj := validProject(work)
	nodes := cycProj["graph"].(map[string]any)["nodes"].([]any)
	nodes = append(nodes,
		map[string]any{"id": "a", "command": "echo a>a", "outputs": []string{"a"}, "depends_on": []string{"b"}},
		map[string]any{"id": "b", "command": "echo b>b", "outputs": []string{"b"}, "depends_on": []string{"a"}},
	)
	cycProj["graph"].(map[string]any)["nodes"] = nodes
	rec, out := doJSON(t, h, "POST", "/v1/validate", cycProj)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("cycle should be 400, got %d", rec.Code)
	}
	errObj, _ := out["error"].(map[string]any)
	if errObj["code"] != "cycle" {
		t.Fatalf("want cycle code, got %v", errObj)
	}

	// Register valid project.
	rec, _ = doJSON(t, h, "POST", "/v1/projects", validProject(work))
	if rec.Code != http.StatusCreated {
		t.Fatalf("register: %d %s", rec.Code, rec.Body.String())
	}
	// Duplicate -> 409.
	rec, _ = doJSON(t, h, "POST", "/v1/projects", validProject(work))
	if rec.Code != http.StatusConflict {
		t.Fatalf("duplicate should be 409, got %d", rec.Code)
	}

	// First build: up is built.
	rec, out = doJSON(t, h, "POST", "/v1/builds", map[string]any{"project_id": "p1"})
	if rec.Code != http.StatusOK {
		t.Fatalf("build: %d %s", rec.Code, rec.Body.String())
	}
	if out["success"] != true {
		t.Fatalf("build not successful: %v", out)
	}
	data, _ := os.ReadFile(filepath.Join(work, "up.txt"))
	if string(data) != "HI\n" {
		t.Fatalf("up.txt = %q", data)
	}

	// Second build: cached.
	rec, out = doJSON(t, h, "POST", "/v1/builds", map[string]any{"project_id": "p1"})
	counts, _ := out["counts"].(map[string]any)
	if counts["cached"].(float64) != 1 {
		t.Fatalf("expected 1 cached node, got counts=%v", counts)
	}

	// Unknown project -> 404.
	rec, _ = doJSON(t, h, "POST", "/v1/builds", map[string]any{"project_id": "ghost"})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown project should 404, got %d", rec.Code)
	}

	// Listing and get.
	rec, out = doJSON(t, h, "GET", "/v1/projects", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list: %d", rec.Code)
	}
	rec, _ = doJSON(t, h, "GET", "/v1/projects/p1", nil)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"id": "p1"`) {
		t.Fatalf("get project: %d %s", rec.Code, rec.Body.String())
	}
}
