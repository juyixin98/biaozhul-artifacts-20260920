package server

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func newTestServer(t *testing.T) (*Server, *httptest.Server) {
	t.Helper()
	srv, err := New(filepath.Join(t.TempDir(), "cache"))
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return srv, ts
}

func post(t *testing.T, url string, body any) (int, []byte) {
	t.Helper()
	data, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(data))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	_, _ = buf.ReadFrom(resp.Body)
	return resp.StatusCode, buf.Bytes()
}

func snapshotDir(t *testing.T, root string) map[string]string {
	t.Helper()
	snap := map[string]string{}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		snap[rel] = fmt.Sprintf("%x:%o", sha256.Sum256(data), info.Mode().Perm())
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return snap
}

func TestHealthz(t *testing.T) {
	_, ts := newTestServer(t)
	resp, err := http.Get(ts.URL + "/v1/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
}

func TestApplyEndToEnd(t *testing.T) {
	_, ts := newTestServer(t)
	work := t.TempDir()
	if err := os.WriteFile(filepath.Join(work, "main.txt"), []byte("hello\nworld\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	patch := `--- a/main.txt
+++ b/main.txt
@@ -1,2 +1,2 @@
 hello
-world
+WORLD
`
	status, body := post(t, ts.URL+"/v1/apply", map[string]any{
		"workdir": work,
		"patch":   patch,
	})
	if status != http.StatusOK {
		t.Fatalf("status = %d, body = %s", status, body)
	}
	var ar applyResponse
	if err := json.Unmarshal(body, &ar); err != nil {
		t.Fatal(err)
	}
	if !ar.OK || len(ar.Changed) != 1 || ar.Changed[0].Path != "main.txt" || ar.Changed[0].Action != "modify" {
		t.Fatalf("response = %+v", ar)
	}
	data, err := os.ReadFile(filepath.Join(work, "main.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "hello\nWORLD\n" {
		t.Fatalf("content = %q", data)
	}
}

// TestApplyFailureLeavesDirectoryByteIdentical is the end-to-end acceptance
// check: a batch whose second patch has wrong context must fail and the work
// directory must be byte-for-byte unchanged.
func TestApplyFailureLeavesDirectoryByteIdentical(t *testing.T) {
	_, ts := newTestServer(t)
	work := t.TempDir()
	files := map[string]string{
		"a.txt":        "alpha\nbeta\n",
		"sub/b.txt":    "one\ntwo\n",
		"sub/c-no-nl":  "no newline at end",
		"bin/data.bin": "\x00\x01\x02\xfe\xff",
	}
	for name, content := range files {
		p := filepath.Join(work, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	before := snapshotDir(t, work)

	good := `--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 alpha
-beta
+BETA
`
	bad := `--- a/sub/b.txt
+++ b/sub/b.txt
@@ -1,2 +1,2 @@
 one
-WRONG CONTEXT
+two
`
	status, body := post(t, ts.URL+"/v1/apply", map[string]any{
		"workdir": work,
		"patches": []string{good, bad},
	})
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, body = %s", status, body)
	}
	var ar applyResponse
	if err := json.Unmarshal(body, &ar); err != nil {
		t.Fatal(err)
	}
	if ar.OK || ar.Error == "" {
		t.Fatalf("response = %+v", ar)
	}
	if !strings.Contains(ar.Error, "sub/b.txt") {
		t.Fatalf("error %q does not name the failing file", ar.Error)
	}

	after := snapshotDir(t, work)
	if len(after) != len(before) {
		t.Fatalf("directory changed: before=%v after=%v", before, after)
	}
	for path, sum := range before {
		if after[path] != sum {
			t.Fatalf("%s changed after failed apply: %s -> %s", path, sum, after[path])
		}
	}
}

func TestApplyRejectsPathTraversal(t *testing.T) {
	_, ts := newTestServer(t)
	work := t.TempDir()
	outside := filepath.Join(filepath.Dir(work), "escape-target.txt")
	patch := `--- /dev/null
+++ b/../escape-target.txt
@@ -0,0 +1 @@
+escaped
`
	status, body := post(t, ts.URL+"/v1/apply", map[string]any{
		"workdir": work,
		"patch":   patch,
	})
	if status != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d, body = %s", status, body)
	}
	if _, err := os.Stat(outside); !os.IsNotExist(err) {
		t.Fatal("path traversal wrote outside the workdir")
	}
}

func TestApplyRejectsRelativeWorkdir(t *testing.T) {
	_, ts := newTestServer(t)
	status, _ := post(t, ts.URL+"/v1/apply", map[string]any{
		"workdir": "relative/path",
		"patch":   "--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+x\n",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("status = %d", status)
	}
}

func TestApplyRejectsWorkdirContainingCache(t *testing.T) {
	// Cache dir inside the workdir must be refused.
	root := t.TempDir()
	srv, err := New(filepath.Join(root, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	status, body := post(t, ts.URL+"/v1/apply", map[string]any{
		"workdir": root,
		"patch":   "--- /dev/null\n+++ b/x.txt\n@@ -0,0 +1 @@\n+x\n",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("status = %d, body = %s", status, body)
	}
}

// TestRunFixtureCommand verifies /v1/test runs exactly the caller-supplied
// argv (no shell) and reports exit code and output.
func TestRunFixtureCommand(t *testing.T) {
	_, ts := newTestServer(t)
	work := t.TempDir()

	var cmd []string
	if runtime.GOOS == "windows" {
		t.Skip("test uses /bin/sh")
	}
	cmd = []string{"/bin/sh", "-c", "echo out; echo err >&2; exit 3"}

	status, body := post(t, ts.URL+"/v1/test", map[string]any{
		"workdir": work,
		"command": cmd,
	})
	if status != http.StatusOK {
		t.Fatalf("status = %d, body = %s", status, body)
	}
	var tr testResponse
	if err := json.Unmarshal(body, &tr); err != nil {
		t.Fatal(err)
	}
	if tr.OK || tr.ExitCode != 3 {
		t.Fatalf("response = %+v", tr)
	}
	if strings.TrimSpace(tr.Stdout) != "out" || strings.TrimSpace(tr.Stderr) != "err" {
		t.Fatalf("stdout=%q stderr=%q", tr.Stdout, tr.Stderr)
	}
}

func TestRunFixtureCommandSuccess(t *testing.T) {
	_, ts := newTestServer(t)
	work := t.TempDir()
	status, body := post(t, ts.URL+"/v1/test", map[string]any{
		"workdir": work,
		"command": []string{"/bin/echo", "hello"},
	})
	if status != http.StatusOK {
		t.Fatalf("status = %d, body = %s", status, body)
	}
	var tr testResponse
	if err := json.Unmarshal(body, &tr); err != nil {
		t.Fatal(err)
	}
	if !tr.OK || tr.ExitCode != 0 || strings.TrimSpace(tr.Stdout) != "hello" {
		t.Fatalf("response = %+v", tr)
	}
}

func TestRunFixtureCommandEmpty(t *testing.T) {
	_, ts := newTestServer(t)
	status, _ := post(t, ts.URL+"/v1/test", map[string]any{
		"workdir": t.TempDir(),
		"command": []string{},
	})
	if status != http.StatusBadRequest {
		t.Fatalf("status = %d", status)
	}
}
