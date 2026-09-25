package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"bis/internal/store"
	"bis/internal/testutil"
)

func newServer(t *testing.T) (*httptest.Server, *store.Store) {
	t.Helper()
	st := testutil.NewStore(t)
	srv := httptest.NewServer(NewService(st, 10*time.Second).Handler())
	t.Cleanup(srv.Close)
	return srv, st
}

// writeFixtureTree writes a complete fixture project (build.json, sources
// and executable tools) into an arbitrary directory outside the store.
func writeFixtureTree(t *testing.T, dir, buildJSON string, sources map[string]string) {
	t.Helper()
	script := func(name, body string) {
		p := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	script("tools/concat.sh", testutil.ConcatTool)
	script("tools/concat_nondet.sh", testutil.NondetTool)
	script("tools/fail.sh", testutil.FailTool)
	for rel, body := range sources {
		p := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "build.json"), []byte(buildJSON), 0o644); err != nil {
		t.Fatal(err)
	}
}

func doJSON(t *testing.T, method, url string, body any) (int, map[string]any) {
	t.Helper()
	var rdr *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	dec := json.NewDecoder(resp.Body)
	_ = dec.Decode(&out)
	if out == nil {
		out = map[string]any{}
	}
	return resp.StatusCode, out
}

func TestFullLifecycleOverHTTP(t *testing.T) {
	srv, _ := newServer(t)
	fixture := filepath.Join(t.TempDir(), "fixture")
	writeFixtureTree(t, fixture, testutil.SharedBuildJSON, testutil.SharedFiles())

	// Import.
	code, body := doJSON(t, http.MethodPut, srv.URL+"/projects/shared-demo",
		map[string]string{"src_dir": fixture})
	if code != http.StatusCreated {
		t.Fatalf("PUT = %d body=%v", code, body)
	}

	// Duplicate import without replace is a conflict.
	if code, _ := doJSON(t, http.MethodPut, srv.URL+"/projects/shared-demo",
		map[string]string{"src_dir": fixture}); code != http.StatusConflict {
		t.Fatalf("duplicate PUT = %d, want 409", code)
	}

	// Build.
	code, body = doJSON(t, http.MethodPost, srv.URL+"/projects/shared-demo/build", nil)
	if code != http.StatusOK {
		t.Fatalf("build = %d body=%v", code, body)
	}
	if body["status"] != "ok" {
		t.Fatalf("build status = %v", body["status"])
	}
	actions := body["actions"].([]any)
	if len(actions) != 3 {
		t.Fatalf("want 3 actions, got %d", len(actions))
	}

	// Second build is fully cached (no commands run).
	_, body = doJSON(t, http.MethodPost, srv.URL+"/projects/shared-demo/build", nil)
	for _, a := range body["actions"].([]any) {
		am := a.(map[string]any)
		if am["status"] != "cached" {
			t.Fatalf("action %v not cached", am["action_id"])
		}
	}

	// Verify: complete.
	code, body = doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/verify", nil)
	if code != http.StatusOK {
		t.Fatalf("verify = %d body=%v", code, body)
	}
	if body["complete"] != true {
		t.Fatalf("verify complete=%v findings=%v", body["complete"], body["findings"])
	}

	// Impact: shared.txt reaches all three actions.
	code, body = doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/impact?path=src/shared.txt", nil)
	if code != http.StatusOK {
		t.Fatalf("impact = %d", code)
	}
	affected := body["affected_actions"].([]any)
	if len(affected) != 3 {
		t.Fatalf("shared.txt impact = %v", affected)
	}

	// Impact: a.txt skips compile_b.
	_, body = doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/impact?path=src/a.txt", nil)
	seenB := false
	for _, x := range body["affected_actions"].([]any) {
		if x == "compile_b" {
			seenB = true
		}
	}
	if seenB {
		t.Fatal("compile_b wrongly affected by a.txt")
	}

	// Records + graph endpoints.
	code, body = doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/records", nil)
	if code != http.StatusOK {
		t.Fatalf("records = %d", code)
	}
	if _, ok := body["records"].(map[string]any)["link"]; !ok {
		t.Fatal("index missing link record")
	}
	code, body = doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/records/link", nil)
	if code != http.StatusOK {
		t.Fatalf("get record = %d", code)
	}
	rec := body["record"].(map[string]any)
	if len(rec["upstreams"].([]any)) != 2 {
		t.Fatal("link record must list two upstreams")
	}
	if _, body := doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/records/nope", nil); body == nil {
		t.Fatal("expected error body for missing record")
	}
	code, _ = doJSON(t, http.MethodGet, srv.URL+"/projects/shared-demo/graph", nil)
	if code != http.StatusOK {
		t.Fatalf("graph = %d", code)
	}

	// Projects listing.
	code, body = doJSON(t, http.MethodGet, srv.URL+"/projects", nil)
	if code != http.StatusOK {
		t.Fatalf("list = %d", code)
	}
	if len(body["projects"].([]any)) != 1 {
		t.Fatalf("projects = %v", body["projects"])
	}
}

func TestReproduceEndpoint(t *testing.T) {
	srv, _ := newServer(t)

	// Deterministic project reproduces.
	fix := filepath.Join(t.TempDir(), "fix-det")
	writeFixtureTree(t, fix, testutil.SharedBuildJSON, testutil.SharedFiles())
	doJSON(t, http.MethodPut, srv.URL+"/projects/shared-demo", map[string]string{"src_dir": fix})
	doJSON(t, http.MethodPost, srv.URL+"/projects/shared-demo/build", nil)
	code, body := doJSON(t, http.MethodPost, srv.URL+"/projects/shared-demo/reproduce", nil)
	if code != http.StatusOK || body["reproduced"] != true {
		t.Fatalf("deterministic reproduce: code=%d body=%v", code, body)
	}

	// Non-deterministic project does not reproduce (409).
	fixN := filepath.Join(t.TempDir(), "fix-nd")
	writeFixtureTree(t, fixN, testutil.NondetBuildJSON, map[string]string{"src/s.txt": "x\n"})
	doJSON(t, http.MethodPut, srv.URL+"/projects/nondet-demo", map[string]string{"src_dir": fixN})
	doJSON(t, http.MethodPost, srv.URL+"/projects/nondet-demo/build", nil)
	code, body = doJSON(t, http.MethodPost, srv.URL+"/projects/nondet-demo/reproduce", nil)
	if code != http.StatusConflict || body["reproduced"] != false {
		t.Fatalf("nondet reproduce: code=%d body=%v", code, body)
	}
}

func TestImportRejectsInvalidAndSymlinks(t *testing.T) {
	srv, _ := newServer(t)

	// Cyclic spec must be rejected at import.
	cyc := filepath.Join(t.TempDir(), "cyc")
	cyclicJSON := `{
	  "name": "cyc",
	  "actions": [
	    {"id": "x", "tool": "tools/concat.sh", "upstream": {"u": "y"}, "outputs": ["ox"]},
	    {"id": "y", "tool": "tools/concat.sh", "upstream": {"u": "x"}, "outputs": ["oy"]}
	  ]
	}`
	writeFixtureTree(t, cyc, cyclicJSON, nil)
	code, body := doJSON(t, http.MethodPut, srv.URL+"/projects/cyc", map[string]string{"src_dir": cyc})
	if code != http.StatusBadRequest || !strings.Contains(body["error"].(string), "cyclic") {
		t.Fatalf("cycle import code=%d body=%v", code, body)
	}

	// Symlink in the project tree is rejected.
	linkDir := filepath.Join(t.TempDir(), "link")
	writeFixtureTree(t, linkDir, testutil.SharedBuildJSON, testutil.SharedFiles())
	if err := os.Symlink("/etc/hostname", filepath.Join(linkDir, "src", "evil.txt")); err != nil {
		t.Fatal(err)
	}
	code, body = doJSON(t, http.MethodPut, srv.URL+"/projects/linked", map[string]string{"src_dir": linkDir})
	if code != http.StatusBadRequest || !strings.Contains(body["error"].(string), "symlink") {
		t.Fatalf("symlink import code=%d body=%v", code, body)
	}

	// Relative src_dir is rejected.
	if code, _ := doJSON(t, http.MethodPut, srv.URL+"/projects/x", map[string]string{"src_dir": "relative/path"}); code != http.StatusBadRequest {
		t.Fatalf("relative src_dir = %d, want 400", code)
	}

	// Unknown project.
	if code, _ := doJSON(t, http.MethodGet, srv.URL+"/projects/ghost/verify", nil); code != http.StatusBadRequest {
		t.Fatalf("unknown project verify = %d", code)
	}
}

func TestHealthz(t *testing.T) {
	srv, _ := newServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz = %d", resp.StatusCode)
	}
}
