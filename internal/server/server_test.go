package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"buildcache/internal/cas"
	"buildcache/internal/store"
)

// fakecc is a real shell script used as the "compiler": it counts its
// invocations, fails while a FAIL flag file exists, and produces an
// executable artifact whose output reflects the declared environment and
// the source contents.
const fakeccTemplate = `#!/bin/sh
if [ "$1" = "--version" ]; then echo "fakecc 1.0"; exit 0; fi
set -e
echo build >> "__COUNT__"
if [ -f "__TASKDIR__/FAIL" ]; then echo "forced build failure" >&2; exit 1; fi
sleep __SLEEP__
payload="version=${APP_VERSION:-unset} src=$(cat main.txt 2>/dev/null)"
{
  echo '#!/bin/sh'
  echo "echo \"$payload\""
} > "$OUT"
chmod +x "$OUT"
`

type testEnv struct {
	srv       *Server
	http      *httptest.Server
	dataDir   string
	ws        string
	taskDir   string
	countFile string
}

func setup(t *testing.T, sleep string) *testEnv {
	t.Helper()
	dataDir := t.TempDir()
	ws := t.TempDir()
	taskDir := filepath.Join(ws, "task")
	if err := os.MkdirAll(taskDir, 0o755); err != nil {
		t.Fatal(err)
	}
	countFile := filepath.Join(dataDir, "build-count")
	script := strings.NewReplacer(
		"__COUNT__", countFile,
		"__TASKDIR__", taskDir,
		"__SLEEP__", sleep,
	).Replace(fakeccTemplate)
	if err := os.WriteFile(filepath.Join(taskDir, "fakecc.sh"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(taskDir, "main.txt"), []byte("v1"), 0o644); err != nil {
		t.Fatal(err)
	}

	st, err := store.Open(filepath.Join(dataDir, "meta.db"))
	if err != nil {
		t.Fatal(err)
	}
	c, err := cas.Open(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := New(st, c, ws, filepath.Join(dataDir, "tmp"))
	if err != nil {
		t.Fatal(err)
	}
	te := &testEnv{srv: srv, http: httptest.NewServer(srv.Handler()), dataDir: dataDir, ws: ws, taskDir: taskDir, countFile: countFile}
	t.Cleanup(func() {
		te.http.Close()
		st.Close()
	})
	return te
}

func (te *testEnv) buildCount(t *testing.T) int {
	t.Helper()
	b, err := os.ReadFile(te.countFile)
	if os.IsNotExist(err) {
		return 0
	}
	if err != nil {
		t.Fatal(err)
	}
	n := strings.Count(strings.TrimSpace(string(b)), "\n") + 1
	if strings.TrimSpace(string(b)) == "" {
		return 0
	}
	return n
}

func (te *testEnv) build(t *testing.T, req map[string]any) (buildResponse, int) {
	t.Helper()
	body, _ := json.Marshal(req)
	resp, err := http.Post(te.http.URL+"/v1/builds", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var br buildResponse
	if err := json.NewDecoder(resp.Body).Decode(&br); err != nil {
		t.Fatal(err)
	}
	return br, resp.StatusCode
}

func baseReq() map[string]any {
	return map[string]any{
		"task_dir": "task",
		"sources":  []string{"main.txt"},
		"command":  "./fakecc.sh",
		"env":      map[string]string{"APP_VERSION": "1"},
	}
}

func TestMissThenHit(t *testing.T) {
	te := setup(t, "0")

	r1, code := te.build(t, baseReq())
	if code != 200 || r1.Cache != "miss" || r1.Status != "success" {
		t.Fatalf("first build: code=%d resp=%+v", code, r1)
	}
	if !strings.Contains(r1.RunOutput, "version=1") {
		t.Fatalf("run output %q should reflect declared env", r1.RunOutput)
	}
	r2, code := te.build(t, baseReq())
	if code != 200 || r2.Cache != "hit" {
		t.Fatalf("second build: code=%d resp=%+v", code, r2)
	}
	if r1.Artifact.Digest != r2.Artifact.Digest {
		t.Fatal("hit must return the same artifact digest")
	}
	if n := te.buildCount(t); n != 1 {
		t.Fatalf("compiler ran %d times, want exactly 1", n)
	}
}

func TestSourceEditInvalidates(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq())
	if err := os.WriteFile(filepath.Join(te.taskDir, "main.txt"), []byte("v2"), 0o644); err != nil {
		t.Fatal(err)
	}
	r2, _ := te.build(t, baseReq())
	if r2.Cache != "miss" || r2.Key == r1.Key {
		t.Fatalf("editing a source must invalidate: r1=%+v r2=%+v", r1, r2)
	}
}

func TestDeclaredEnvChangeInvalidates(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq())
	req := baseReq()
	req["env"] = map[string]string{"APP_VERSION": "2"}
	r2, _ := te.build(t, req)
	if r2.Cache != "miss" || r2.Key == r1.Key {
		t.Fatalf("changing declared env must invalidate")
	}
	if !strings.Contains(r2.RunOutput, "version=2") {
		t.Fatalf("run output %q should reflect new env", r2.RunOutput)
	}
}

// TestEnvOmissionCannotProduceStaleHit: a client that forgets to declare
// an environment variable gets a different key, never the stale result
// produced with the variable set.
func TestEnvOmissionCannotProduceStaleHit(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq()) // APP_VERSION=1 declared

	req := baseReq()
	delete(req, "env") // same sources, same command, env omitted
	r2, code := te.build(t, req)
	if code != 200 || r2.Cache != "miss" {
		t.Fatalf("omitting a declared env var must not hit the old entry: %+v", r2)
	}
	if r2.Key == r1.Key {
		t.Fatal("key must differ when the environment declaration differs")
	}
	if !strings.Contains(r2.RunOutput, "version=unset") {
		t.Fatalf("undeclared env must not leak into the build, got %q", r2.RunOutput)
	}
	if n := te.buildCount(t); n != 2 {
		t.Fatalf("compiler ran %d times, want 2 (no stale reuse)", n)
	}
}

func TestConcurrentSameKeySinglePublisher(t *testing.T) {
	te := setup(t, "0.3")

	const n = 8
	var wg sync.WaitGroup
	results := make([]buildResponse, n)
	codes := make([]int, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], codes[i] = te.build(t, baseReq())
		}(i)
	}
	wg.Wait()

	misses, hits := 0, 0
	for i, r := range results {
		if codes[i] != 200 {
			t.Fatalf("request %d failed: code=%d resp=%+v", i, codes[i], r)
		}
		switch r.Cache {
		case "miss":
			misses++
		case "hit":
			hits++
		}
		if r.Artifact.Digest != results[0].Artifact.Digest {
			t.Fatal("all concurrent clients must receive the same artifact")
		}
	}
	if misses != 1 || hits != n-1 {
		t.Fatalf("got %d misses and %d hits, want 1 miss and %d hits", misses, hits, n-1)
	}
	if c := te.buildCount(t); c != 1 {
		t.Fatalf("compiler ran %d times for one key, want exactly 1", c)
	}
}

func TestFailedBuildNeverCachedAsSuccess(t *testing.T) {
	te := setup(t, "0")
	flag := filepath.Join(te.taskDir, "FAIL")
	if err := os.WriteFile(flag, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	r1, code1 := te.build(t, baseReq())
	if code1 != 500 || r1.Status != "failed" {
		t.Fatalf("failing build: code=%d resp=%+v", code1, r1)
	}
	r2, code2 := te.build(t, baseReq())
	if code2 != 500 || r2.Cache == "hit" {
		t.Fatalf("failed build must never be served as a hit: code=%d resp=%+v", code2, r2)
	}
	if n := te.buildCount(t); n != 2 {
		t.Fatalf("failed builds must be retried, compiler ran %d times, want 2", n)
	}

	// Fix the build; the same key must now succeed (re-acquired lease).
	if err := os.Remove(flag); err != nil {
		t.Fatal(err)
	}
	r3, code3 := te.build(t, baseReq())
	if code3 != 200 || r3.Cache != "miss" || r3.Status != "success" {
		t.Fatalf("fixed build: code=%d resp=%+v", code3, r3)
	}
	r4, _ := te.build(t, baseReq())
	if r4.Cache != "hit" {
		t.Fatalf("successful rebuild must be cached: %+v", r4)
	}
}

func TestArtifactLossTriggersRebuild(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq())

	// Lose the artifact bytes; metadata still claims success.
	if err := os.Remove(filepath.Join(te.dataDir, "cas", r1.Artifact.Digest)); err != nil {
		t.Fatal(err)
	}
	r2, code := te.build(t, baseReq())
	if code != 200 || r2.Cache != "miss" {
		t.Fatalf("lost artifact must trigger a rebuild: code=%d resp=%+v", code, r2)
	}
	if n := te.buildCount(t); n != 2 {
		t.Fatalf("compiler ran %d times, want 2", n)
	}
	r3, _ := te.build(t, baseReq())
	if r3.Cache != "hit" {
		t.Fatalf("rebuilt entry must hit again: %+v", r3)
	}
}

func TestCorruptArtifactQuarantined(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq())

	// Corrupt the stored bytes.
	p := filepath.Join(te.dataDir, "cas", r1.Artifact.Digest)
	f, err := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	f.WriteString("garbage")
	f.Close()

	r2, code := te.build(t, baseReq())
	if code != 200 || r2.Cache != "miss" {
		t.Fatalf("corrupt artifact must be quarantined and rebuilt: code=%d resp=%+v", code, r2)
	}
	entries, err := os.ReadDir(filepath.Join(te.dataDir, "quarantine"))
	if err != nil || len(entries) != 1 {
		t.Fatalf("quarantine dir should hold the corrupt object: entries=%v err=%v", entries, err)
	}
	// The rebuilt artifact verifies and is served again.
	r3, _ := te.build(t, baseReq())
	if r3.Cache != "hit" {
		t.Fatalf("rebuilt entry must hit again: %+v", r3)
	}
}

func TestRestartKeepsCache(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq())
	te.http.Close()

	// Simulate a full server restart over the same data directory.
	st, err := store.Open(filepath.Join(te.dataDir, "meta.db"))
	if err != nil {
		t.Fatal(err)
	}
	c, err := cas.Open(te.dataDir)
	if err != nil {
		t.Fatal(err)
	}
	srv2, err := New(st, c, te.ws, filepath.Join(te.dataDir, "tmp"))
	if err != nil {
		t.Fatal(err)
	}
	http2 := httptest.NewServer(srv2.Handler())
	defer http2.Close()
	defer st.Close()

	body, _ := json.Marshal(baseReq())
	resp, err := http.Post(http2.URL+"/v1/builds", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var r2 buildResponse
	json.NewDecoder(resp.Body).Decode(&r2)
	if r2.Cache != "hit" || r2.Key != r1.Key || r2.Artifact.Digest != r1.Artifact.Digest {
		t.Fatalf("cache must survive restart: %+v", r2)
	}
	if n := te.buildCount(t); n != 1 {
		t.Fatalf("compiler ran %d times after restart, want 1", n)
	}
}

func TestMissingSourceReportedDistinctly(t *testing.T) {
	te := setup(t, "0")
	req := baseReq()
	req["sources"] = []string{"main.txt", "nope.txt"}
	r1, code := te.build(t, req)
	if code != 422 {
		t.Fatalf("missing source must be a 422, got %d (%+v)", code, r1)
	}
	if len(r1.Missing) != 1 || r1.Missing[0] != "nope.txt" {
		t.Fatalf("missing files must be reported: %+v", r1)
	}

	// An empty file with the same name is a different, buildable input.
	if err := os.WriteFile(filepath.Join(te.taskDir, "nope.txt"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	r2, code := te.build(t, req)
	if code != 200 || r2.Status != "success" {
		t.Fatalf("empty source file must build fine: code=%d resp=%+v", code, r2)
	}
	if r1.Key == r2.Key {
		t.Fatal("missing file and empty file must not share a cache key")
	}
}

func TestAuditShowsBoundInputs(t *testing.T) {
	te := setup(t, "0")
	r1, _ := te.build(t, baseReq())
	resp, err := http.Get(te.http.URL + "/v1/audit/" + r1.Key)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var audit struct {
		Key    string `json:"key"`
		Status string `json:"status"`
		Inputs struct {
			Env      map[string]string `json:"env"`
			Platform string            `json:"platform"`
			Sources  []struct {
				Path  string `json:"path"`
				State string `json:"state"`
			} `json:"sources"`
			Toolchain struct {
				BinarySHA256 string `json:"binary_sha256"`
			} `json:"toolchain"`
		} `json:"inputs"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&audit); err != nil {
		t.Fatal(err)
	}
	if audit.Inputs.Env["APP_VERSION"] != "1" {
		t.Fatalf("audit must show the declared env: %+v", audit.Inputs)
	}
	if len(audit.Inputs.Sources) != 1 || audit.Inputs.Sources[0].State != "present" {
		t.Fatalf("audit must show the manifest: %+v", audit.Inputs.Sources)
	}
	if audit.Inputs.Toolchain.BinarySHA256 == "" || audit.Inputs.Platform == "" {
		t.Fatal("audit must show toolchain digest and platform")
	}
}

// TestRealGCCBuild compiles a real C program with the system gcc and
// proves hit + invalidation through a header selected via declared CPATH.
func TestRealGCCBuild(t *testing.T) {
	if _, err := exec.LookPath("gcc"); err != nil {
		t.Skip("gcc not available")
	}
	dataDir := t.TempDir()
	ws, err := filepath.Abs("testdata")
	if err != nil {
		t.Fatal(err)
	}
	st, err := store.Open(filepath.Join(dataDir, "meta.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	c, err := cas.Open(dataDir)
	if err != nil {
		t.Fatal(err)
	}
	srv, err := New(st, c, ws, filepath.Join(dataDir, "tmp"))
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	gccReq := func(cpath string) map[string]any {
		r := map[string]any{
			"task_dir": "hello",
			"sources":  []string{"main.c", "util.c", "util.h"},
			"command":  "gcc -O2 -Wall -Werror -o \"$OUT\" main.c util.c",
		}
		if cpath != "" {
			r["env"] = map[string]string{"CPATH": cpath}
		}
		return r
	}
	post := func(req map[string]any) (buildResponse, int) {
		body, _ := json.Marshal(req)
		resp, err := http.Post(ts.URL+"/v1/builds", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var br buildResponse
		json.NewDecoder(resp.Body).Decode(&br)
		return br, resp.StatusCode
	}

	r1, code := post(gccReq("include-v1"))
	if code != 200 || r1.Cache != "miss" || !strings.Contains(r1.RunOutput, "app_version=1.0.0") {
		t.Fatalf("v1 build: code=%d resp=%+v", code, r1)
	}
	r2, _ := post(gccReq("include-v1"))
	if r2.Cache != "hit" || r2.Artifact.Digest != r1.Artifact.Digest {
		t.Fatalf("identical request must hit: %+v", r2)
	}
	r3, _ := post(gccReq("include-v2"))
	if r3.Cache != "miss" || r3.Key == r1.Key || !strings.Contains(r3.RunOutput, "app_version=2.0.0") {
		t.Fatalf("env change must invalidate: %+v", r3)
	}
	// Without the declared CPATH the header is missing: the build fails
	// honestly instead of silently reusing a stale artifact.
	r4, code := post(gccReq(""))
	if code != 500 || r4.Status != "failed" {
		t.Fatalf("missing env must fail honestly, not hit stale: code=%d resp=%+v", code, r4)
	}
	fmt.Println("real gcc outputs:", r1.RunOutput, r3.RunOutput)
}
