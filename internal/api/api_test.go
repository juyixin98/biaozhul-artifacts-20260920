package api_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"ociarch/internal/api"
	"ociarch/internal/fixture"
	"ociarch/internal/oci"
	"ociarch/internal/store"
)

type env struct {
	t   *testing.T
	srv *httptest.Server
	dir string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	st, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(api.NewRouter(st))
	t.Cleanup(func() { srv.Close(); st.Close() })
	return &env{t: t, srv: srv, dir: t.TempDir()}
}

func (e *env) buildFixture(name string) (dir string, root string) {
	e.t.Helper()
	dir = filepath.Join(e.dir, name)
	root, err := fixture.Build(dir, name)
	if err != nil {
		e.t.Fatal(err)
	}
	return dir, root
}

func (e *env) post(path string, body any) (int, map[string]any) {
	e.t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(e.srv.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func (e *env) get(path string) (int, []byte) {
	e.t.Helper()
	resp, err := http.Get(e.srv.URL + path)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	_, _ = buf.ReadFrom(resp.Body)
	return resp.StatusCode, buf.Bytes()
}

func (e *env) importLayout(dir, tag string) (int, map[string]any) {
	return e.post("/api/v1/import", map[string]any{"path": dir, "tag": tag})
}

func resolveBody(ref, os, arch, variant string) map[string]any {
	return map[string]any{
		"ref": ref,
		"platform": map[string]string{
			"os": os, "architecture": arch, "variant": variant,
		},
	}
}

func TestImportAndResolveTwoArch(t *testing.T) {
	e := newEnv(t)
	dir, root := e.buildFixture("two-arch")

	code, out := e.importLayout(dir, "demo/app:1.0")
	if code != http.StatusCreated {
		t.Fatalf("import: want 201, got %d: %v", code, out)
	}
	if out["root_digest"] != root {
		t.Fatalf("root digest mismatch: %v", out)
	}

	code, res := e.post("/api/v1/resolve", resolveBody("demo/app:1.0", "linux", "amd64", ""))
	if code != http.StatusOK {
		t.Fatalf("resolve amd64: want 200, got %d: %v", code, res)
	}
	if res["status"] != "resolved" || res["root_digest"] != root {
		t.Fatalf("bad resolution: %v", res)
	}
	chain, _ := res["chain"].([]any)
	if len(chain) != 5 { // root -> manifest -> config -> 2 layers
		t.Fatalf("want chain of 5, got %d: %v", len(chain), res)
	}
	if chain[0].(map[string]any)["kind"] != "root" ||
		chain[1].(map[string]any)["kind"] != "manifest" ||
		chain[2].(map[string]any)["kind"] != "config" {
		t.Fatalf("chain ordering wrong: %v", chain)
	}
	amdSel := res["selected_digest"].(string)

	code, res2 := e.post("/api/v1/resolve", resolveBody("demo/app:1.0", "linux", "arm64", ""))
	if code != http.StatusOK {
		t.Fatalf("resolve arm64: want 200, got %d: %v", code, res2)
	}
	if res2["selected_digest"] == amdSel {
		t.Fatal("arm64 and amd64 resolved to the same manifest")
	}

	// Resolve by digest directly: same artifact, no tag involved.
	code, res3 := e.post("/api/v1/resolve", resolveBody(root, "linux", "amd64", ""))
	if code != http.StatusOK || res3["selected_digest"] != amdSel {
		t.Fatalf("resolve by digest: got %d %v", code, res3)
	}
}

func TestImportBadConfigPlatformRejected(t *testing.T) {
	e := newEnv(t)
	dir, _ := e.buildFixture("bad-config")
	code, out := e.importLayout(dir, "demo/bad:1")
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("want 422, got %d: %v", code, out)
	}
	msg := out["error"].(map[string]any)["message"].(string)
	if !strings.Contains(msg, "does not match descriptor platform") {
		t.Fatalf("unexpected error message: %s", msg)
	}
}

func TestImportMissingLayerRejected(t *testing.T) {
	e := newEnv(t)
	dir, _ := e.buildFixture("missing-layer")
	code, out := e.importLayout(dir, "demo/broken:1")
	if code != http.StatusUnprocessableEntity {
		t.Fatalf("want 422, got %d: %v", code, out)
	}
	msg := out["error"].(map[string]any)["message"].(string)
	if !strings.Contains(msg, "layer 1") {
		t.Fatalf("unexpected error message: %s", msg)
	}
}

func TestResolveAmbiguous(t *testing.T) {
	e := newEnv(t)
	dir, _ := e.buildFixture("ambiguous")
	if code, out := e.importLayout(dir, "demo/amb:1"); code != http.StatusCreated {
		t.Fatalf("import: %d %v", code, out)
	}
	code, out := e.post("/api/v1/resolve", resolveBody("demo/amb:1", "linux", "amd64", ""))
	if code != http.StatusConflict {
		t.Fatalf("want 409, got %d: %v", code, out)
	}
	if out["error"].(map[string]any)["code"] != "ambiguous" {
		t.Fatalf("want ambiguous error, got %v", out)
	}
	res := out["resolution"].(map[string]any)
	if res["status"] != "ambiguous" {
		t.Fatalf("resolution status: %v", res)
	}
}

func TestResolveVariantRules(t *testing.T) {
	e := newEnv(t)
	dir, _ := e.buildFixture("arm-variants")
	if code, out := e.importLayout(dir, "demo/arm:1"); code != http.StatusCreated {
		t.Fatalf("import: %d %v", code, out)
	}

	// Exact variant match works.
	code, out := e.post("/api/v1/resolve", resolveBody("demo/arm:1", "linux", "arm", "v7"))
	if code != http.StatusOK {
		t.Fatalf("resolve v7: want 200, got %d: %v", code, out)
	}

	// No variant requested, no variant-less manifest: ambiguity, not random.
	code, out = e.post("/api/v1/resolve", resolveBody("demo/arm:1", "linux", "arm", ""))
	if code != http.StatusConflict {
		t.Fatalf("want 409, got %d: %v", code, out)
	}
}

func TestResolveNoMatch(t *testing.T) {
	e := newEnv(t)
	dir, _ := e.buildFixture("two-arch")
	if code, out := e.importLayout(dir, "demo/app:2"); code != http.StatusCreated {
		t.Fatalf("import: %d %v", code, out)
	}
	code, out := e.post("/api/v1/resolve", resolveBody("demo/app:2", "windows", "amd64", ""))
	if code != http.StatusNotFound {
		t.Fatalf("want 404, got %d: %v", code, out)
	}
	if out["resolution"].(map[string]any)["status"] != "no_match" {
		t.Fatalf("resolution status: %v", out)
	}
}

func TestUnknownTagAndDigest(t *testing.T) {
	e := newEnv(t)
	if code, _ := e.post("/api/v1/resolve", resolveBody("no/such:tag", "linux", "amd64", "")); code != http.StatusNotFound {
		t.Fatalf("unknown tag: want 404, got %d", code)
	}
	ghost := "sha256:" + strings.Repeat("1", 64)
	if code, _ := e.post("/api/v1/resolve", resolveBody(ghost, "linux", "amd64", "")); code != http.StatusNotFound {
		t.Fatalf("unknown digest: want 404, got %d", code)
	}
}

// TestTagMoveResolutionBoundToDigest proves tags are movable pointers while
// resolution tasks stay bound to the digest they resolved: re-importing a
// different artifact under the same tag must not rewrite history, and the
// same tag name must never be treated as the same artifact.
func TestTagMoveResolutionBoundToDigest(t *testing.T) {
	e := newEnv(t)

	dir1, root1 := e.buildFixture("two-arch")
	if code, out := e.importLayout(dir1, "demo/app:latest"); code != http.StatusCreated {
		t.Fatalf("import v1: %d %v", code, out)
	}
	code, res1 := e.post("/api/v1/resolve", resolveBody("demo/app:latest", "linux", "amd64", ""))
	if code != http.StatusOK {
		t.Fatalf("resolve v1: %d %v", code, res1)
	}
	id1 := int64(res1["id"].(float64))

	// Build a DIFFERENT artifact (different layer bytes) and move the tag.
	dir2 := filepath.Join(e.dir, "v2")
	b := fixture.New(dir2)
	if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
		[][]byte{[]byte("amd64 v2 layer 1\n"), []byte("amd64 v2 layer 2\n")}, -1); err != nil {
		t.Fatal(err)
	}
	if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "arm64"}, nil,
		[][]byte{[]byte("arm64 v2 layer 1\n"), []byte("arm64 v2 layer 2\n")}, -1); err != nil {
		t.Fatal(err)
	}
	root2, err := b.WriteIndex()
	if err != nil {
		t.Fatal(err)
	}
	if root2 == root1 {
		t.Fatal("fixture error: v2 digest must differ from v1")
	}
	if code, out := e.importLayout(dir2, "demo/app:latest"); code != http.StatusCreated {
		t.Fatalf("import v2: %d %v", code, out)
	}

	// New resolve under the same tag binds the NEW digest.
	code, res2 := e.post("/api/v1/resolve", resolveBody("demo/app:latest", "linux", "amd64", ""))
	if code != http.StatusOK {
		t.Fatalf("resolve v2: %d %v", code, res2)
	}
	if res2["root_digest"] != root2 {
		t.Fatalf("v2 resolution bound to wrong digest: %v", res2)
	}

	// The old resolution is untouched: still root1, with its frozen chain.
	code, body := e.get(fmt.Sprintf("/api/v1/resolutions/%d", id1))
	if code != http.StatusOK {
		t.Fatalf("get resolution: %d", code)
	}
	var old map[string]any
	if err := json.Unmarshal(body, &old); err != nil {
		t.Fatal(err)
	}
	if old["root_digest"] != root1 {
		t.Fatalf("old resolution mutated after tag move: %v", old)
	}
	if len(old["chain"].([]any)) != 5 {
		t.Fatalf("old resolution chain lost: %v", old)
	}

	// Tag history shows the move: two distinct digests under one name.
	code, body = e.get("/api/v1/tags/demo/app:latest/history")
	if code != http.StatusOK {
		t.Fatalf("tag history: %d", code)
	}
	var hist []map[string]any
	if err := json.Unmarshal(body, &hist); err != nil {
		t.Fatal(err)
	}
	if len(hist) != 2 || hist[0]["digest"] != root1 || hist[1]["digest"] != root2 {
		t.Fatalf("tag history wrong: %v", hist)
	}
}
