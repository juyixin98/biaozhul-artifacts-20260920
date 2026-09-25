package api

import (
	"bytes"
	"depscan/internal/service"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

type result struct {
	Graph           map[string][]string `json:"graph"`
	Cycles          [][]string          `json:"cycles"`
	Unresolved      []map[string]any    `json:"unresolved"`
	AffectedFiles   []string            `json:"affectedFiles"`
	AffectedTargets []string            `json:"affectedTargets"`
	FilesScanned    int                 `json:"filesScanned"`
	CachePath       string              `json:"cachePath"`
}

func mustNewService(t *testing.T, cacheDir string) *service.Service {
	t.Helper()
	svc, err := service.NewService(cacheDir)
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func TestHTTPFlow(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	for rel, content := range map[string]string{
		"base.h":      "#define B 1\n",
		"sub/local.h": "// local header\n",
		"main.c":      "#include \"base.h\"\n// #include \"fake.h\"\n#include \"sub/local.h\"\n",
	} {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	svc := mustNewService(t, filepath.Join(t.TempDir(), "cache"))
	ts := httptest.NewServer(NewServer(svc).Handler())
	defer ts.Close()

	// Health.
	resp, err := http.Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz status = %d", resp.StatusCode)
	}
	resp.Body.Close()

	// Full scan.
	full := post(t, ts, "/v1/scan", map[string]any{
		"sourceRoot": root,
		"targets":    []map[string]any{{"name": "app", "sources": []string{"main.c"}}},
	})
	if full.Graph["main.c"] == nil {
		t.Fatalf("main.c missing from graph: %v", full.Graph)
	}
	if len(full.Graph["main.c"]) != 2 {
		t.Errorf("main.c deps = %v, want 2 (comment pseudo-include ignored)", full.Graph["main.c"])
	}

	// Incremental: change base.h -> main.c is affected, target app is affected.
	inc := post(t, ts, "/v1/scan/incremental", map[string]any{
		"sourceRoot": root,
		"targets":    []map[string]any{{"name": "app", "sources": []string{"main.c"}}},
		"changed":    []string{"base.h"},
	})
	wantFiles := "base.h,main.c"
	if got := join(inc.AffectedFiles); got != wantFiles {
		t.Errorf("affectedFiles = %q, want %q", got, wantFiles)
	}
	if len(inc.AffectedTargets) != 1 || inc.AffectedTargets[0] != "app" {
		t.Errorf("affectedTargets = %v, want [app]", inc.AffectedTargets)
	}

	// GET cached graph.
	resp, err = http.Get(ts.URL + "/v1/graph?sourceRoot=" + root)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("GET graph status = %d", resp.StatusCode)
	}
	var cached struct {
		Graph map[string][]string `json:"graph"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&cached); err != nil {
		t.Fatal(err)
	}
	if cached.Graph["main.c"] == nil {
		t.Error("cached graph missing main.c")
	}
}

func TestHTTPMacroIncludeReturns422(t *testing.T) {
	root := filepath.Join(t.TempDir(), "proj")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "a.c"), []byte("#include DYNAMIC\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	svc := mustNewService(t, filepath.Join(t.TempDir(), "cache"))
	ts := httptest.NewServer(NewServer(svc).Handler())
	defer ts.Close()

	body, _ := json.Marshal(map[string]any{"sourceRoot": root})
	resp, err := http.Post(ts.URL+"/v1/scan", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Errorf("status = %d, want 422 for macro-generated include", resp.StatusCode)
	}
	var errBody map[string]string
	_ = json.NewDecoder(resp.Body).Decode(&errBody)
	if errBody["error"] == "" {
		t.Error("expected error message in body")
	}
}

func TestHTTPBadJSON(t *testing.T) {
	svc := mustNewService(t, filepath.Join(t.TempDir(), "cache"))
	ts := httptest.NewServer(NewServer(svc).Handler())
	defer ts.Close()

	resp, err := http.Post(ts.URL+"/v1/scan", "application/json", bytes.NewReader([]byte("{not json")))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", resp.StatusCode)
	}
}

func post(t *testing.T, ts *httptest.Server, path string, body map[string]any) result {
	t.Helper()
	raw, _ := json.Marshal(body)
	resp, err := http.Post(ts.URL+path, "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var e map[string]string
		_ = json.NewDecoder(resp.Body).Decode(&e)
		t.Fatalf("POST %s status %d: %s", path, resp.StatusCode, e["error"])
	}
	var res result
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		t.Fatal(err)
	}
	return res
}

func join(xs []string) string {
	out := ""
	for i, x := range xs {
		if i > 0 {
			out += ","
		}
		out += x
	}
	return out
}
