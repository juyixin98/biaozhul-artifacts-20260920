package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"buildprovenance/internal/executor"
	"buildprovenance/internal/httpapi"
	"buildprovenance/internal/provenance"
	"buildprovenance/internal/service"
)

type harness struct {
	t      *testing.T
	server *httptest.Server
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	root := t.TempDir()
	srcCAS, err := provenance.NewCAS(filepath.Join(root, "sources"))
	if err != nil {
		t.Fatal(err)
	}
	artCAS, err := provenance.NewCAS(filepath.Join(root, "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	sources := provenance.NewSourceRegistry(srcCAS)
	artifacts := provenance.NewArtifactRegistry(artCAS)
	key := bytes.Repeat([]byte{0x77}, 32)
	attLog, err := provenance.OpenLog(filepath.Join(root, "log.jsonl"), key)
	if err != nil {
		t.Fatal(err)
	}
	repoRoot := findRepoRoot(t)
	policy, err := executor.NewPolicy(executor.DefaultInterpreter, filepath.Join(repoRoot, "fixtures"))
	if err != nil {
		t.Fatal(err)
	}
	svc, err := service.New(service.Config{
		Policy: policy, Sources: sources, Artifacts: artifacts,
		Log: attLog, WorkRoot: filepath.Join(root, "work"),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &harness{t: t, server: httptest.NewServer(httpapi.NewServer(svc).Handler())}
}

func findRepoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("go.mod not found")
		}
		dir = parent
	}
}

func (h *harness) postJSON(path string, body any) (int, map[string]any) {
	h.t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		h.t.Fatal(err)
	}
	resp, err := http.Post(h.server.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	return resp.StatusCode, decode(h, resp)
}

func (h *harness) get(path string) (int, map[string]any) {
	h.t.Helper()
	resp, err := http.Get(h.server.URL + path)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	return resp.StatusCode, decode(h, resp)
}

func decode(h *harness, resp *http.Response) map[string]any {
	h.t.Helper()
	var m map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		// Body may be empty on binary endpoints; callers of those use raw get.
		return map[string]any{"_decodeError": err.Error(), "_status": resp.StatusCode}
	}
	m["_status"] = float64(resp.StatusCode)
	return m
}

func TestEndToEndBuildVerifyImpact(t *testing.T) {
	h := newHarness(t)
	defer h.server.Close()

	// Health.
	if code, m := h.get("/v1/health"); code != 200 || m["status"] != "ok" {
		t.Fatalf("health: %d %v", code, m)
	}

	// Register sources.
	for p, content := range map[string]string{
		"src/common.js": "function greet(n){return 'hi '+n;}\n",
		"src/appA.js":   "console.log(greet('a'));\n",
	} {
		if code, m := h.postJSON("/v1/sources", map[string]any{"path": p, "content": content}); code != 201 {
			t.Fatalf("register source %s: %d %v", p, code, m)
		}
	}

	// Register the compile tool.
	if code, m := h.postJSON("/v1/tools", map[string]any{
		"name":    "compile_lib",
		"command": []string{"bash", "compile_lib.sh"},
	}); code != 201 || m["digest"] == nil {
		t.Fatalf("register tool: %d %v", code, m)
	}

	// Build the library.
	code, m := h.postJSON("/v1/actions", map[string]any{
		"tool": "compile_lib",
		"inputs": []map[string]string{
			{"slot": "COMMON", "sourcePath": "src/common.js"},
			{"slot": "PART", "sourcePath": "src/appA.js"},
		},
		"outputs": []string{"LIB"},
	})
	if code != 201 {
		t.Fatalf("execute: %d %v", code, m)
	}
	arts, _ := m["artifacts"].([]any)
	if len(arts) != 1 {
		t.Fatalf("expected 1 artifact, got %v", m)
	}
	artID, _ := arts[0].(map[string]any)["id"].(string)
	if !strings.HasPrefix(artID, "art_") {
		t.Fatalf("bad artifact id %q", artID)
	}

	// Verify: complete.
	if code, m := h.postJSON("/v1/artifacts/"+artID+"/verify", map[string]any{}); code != 200 || m["complete"] != true {
		t.Fatalf("verify: %d %v", code, m)
	}

	// Reproduce: reproducible.
	if code, m := h.postJSON("/v1/artifacts/"+artID+"/reproduce", map[string]any{}); code != 200 || m["reproducible"] != true {
		t.Fatalf("reproduce: %d %v", code, m)
	}

	// Impact: the source change affects the artifact.
	if code, m := h.get("/v1/impact?source=src/common.js"); code != 200 {
		t.Fatalf("impact: %d %v", code, m)
	} else {
		aff, _ := m["affected"].([]any)
		if len(aff) != 1 || aff[0] != artID {
			t.Fatalf("impact affected=%v", aff)
		}
	}

	// 404 path.
	if code, _ := h.get("/v1/artifacts/art_doesnotexist"); code != 404 {
		t.Fatalf("expected 404, got %d", code)
	}

	// Disallowed action via API (path traversal).
	if code, m := h.postJSON("/v1/tools", map[string]any{
		"name": "evil", "command": []string{"bash", "../../etc/passwd"},
	}); code != 400 {
		t.Fatalf("expected 400 for traversal tool, got %d %v", code, m)
	}
}
