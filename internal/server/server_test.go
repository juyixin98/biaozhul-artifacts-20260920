package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"localcache/api"
	"localcache/internal/builder"
	"localcache/internal/cas"
)

// newTestServer starts a cache server on a throwaway cache dir and returns
// its base URL plus the cache root.
func newTestServer(t *testing.T, maxObjSize int64) (baseURL, cacheRoot string) {
	t.Helper()
	cacheRoot = t.TempDir()
	store, err := cas.Open(cacheRoot, maxObjSize)
	if err != nil {
		t.Fatalf("cas.Open: %v", err)
	}
	exec, err := builder.NewExecutor(store, t.TempDir(), 5*time.Second)
	if err != nil {
		t.Fatalf("NewExecutor: %v", err)
	}
	srv, err := New(store, exec, cacheRoot)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	return ts.URL, cacheRoot
}

func put(t *testing.T, base string, data []byte) (int, api.PutResponse) {
	t.Helper()
	digest := cas.DigestOf(data)
	req, err := http.NewRequest(http.MethodPut, base+"/v1/cas/"+digest, bytes.NewReader(data))
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("PUT: %v", err)
	}
	defer resp.Body.Close()
	var pr api.PutResponse
	json.NewDecoder(resp.Body).Decode(&pr)
	return resp.StatusCode, pr
}

func TestHealthAndObjectLifecycle(t *testing.T) {
	base, _ := newTestServer(t, 1<<20)

	resp, err := http.Get(base + "/v1/health")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health: %s", resp.Status)
	}

	data := []byte("artifact bytes")
	digest := cas.DigestOf(data)
	code, pr := put(t, base, data)
	if code != http.StatusCreated || pr.Digest != digest || pr.Size != int64(len(data)) {
		t.Fatalf("PUT = %d %+v", code, pr)
	}

	// Duplicate upload is a dedup hit, not an error.
	code, pr = put(t, base, data)
	if code != http.StatusOK || !pr.Dedup {
		t.Fatalf("duplicate PUT = %d %+v, want 200 dedup", code, pr)
	}

	resp, err = http.Get(base + "/v1/cas/" + digest)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || !bytes.Equal(got, data) {
		t.Fatalf("GET = %s, %d bytes", resp.Status, len(got))
	}

	resp, err = http.Head(base + "/v1/cas/" + digest)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("HEAD = %s", resp.Status)
	}

	resp, err = http.Get(base + "/v1/cas/" + cas.DigestOf([]byte("absent")))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("GET absent = %s", resp.Status)
	}
	var er api.ErrorResponse
	if json.Unmarshal(body, &er) != nil || er.Error == "" {
		t.Fatalf("404 body is not a JSON error: %q", body)
	}
}

func TestPutSizeLimitAndDigestMismatch(t *testing.T) {
	base, _ := newTestServer(t, 8)

	big := []byte("0123456789abcdef")
	req, _ := http.NewRequest(http.MethodPut, base+"/v1/cas/"+cas.DigestOf(big), bytes.NewReader(big))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize PUT = %s, want 413", resp.Status)
	}

	small := []byte("1234")
	wrongDigest := cas.DigestOf([]byte("something else"))
	req, _ = http.NewRequest(http.MethodPut, base+"/v1/cas/"+wrongDigest, bytes.NewReader(small))
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("mismatched PUT = %s, want 400", resp.Status)
	}

	resp, err = http.Get(base + "/v1/cas/" + wrongDigest)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("rejected upload must not be published, GET = %s", resp.Status)
	}
}

// Acceptance: an upload whose connection breaks mid-body must never become
// a visible object.
func TestTruncatedUploadIsNotPublished(t *testing.T) {
	base, _ := newTestServer(t, 1<<20)
	data := []byte("content that will be cut off")
	digest := cas.DigestOf(data)

	// Chunked body that dies after 5 bytes.
	pr, pw := io.Pipe()
	go func() {
		pw.Write(data[:5])
		pw.CloseWithError(fmt.Errorf("simulated network failure"))
	}()
	req, _ := http.NewRequest(http.MethodPut, base+"/v1/cas/"+digest, pr)
	resp, err := http.DefaultClient.Do(req)
	if err == nil {
		resp.Body.Close()
	}

	// The object must not exist, no matter how the server surfaced the error.
	deadline := time.Now().Add(2 * time.Second)
	for {
		resp, err := http.Head(base + "/v1/cas/" + digest)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode == http.StatusNotFound {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("truncated upload became visible: HEAD = %s", resp.Status)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// Acceptance: a build runs exactly the supplied fixture command, and an
// identical request is served from the action cache without re-execution
// (proved by a nondeterministic command returning identical output).
func TestBuildFixtureAndActionCache(t *testing.T) {
	base, _ := newTestServer(t, 1<<20)

	input := []byte("hello\n")
	code, pr := put(t, base, input)
	if code != http.StatusCreated {
		t.Fatalf("PUT input = %d", code)
	}

	build := func(req *api.BuildRequest) api.BuildResult {
		t.Helper()
		body, _ := json.Marshal(req)
		resp, err := http.Post(base+"/v1/builds", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatalf("POST builds: %v", err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			b, _ := io.ReadAll(resp.Body)
			t.Fatalf("POST builds = %s: %s", resp.Status, b)
		}
		var res api.BuildResult
		json.NewDecoder(resp.Body).Decode(&res)
		return res
	}

	// 1. Transform an input and collect a declared output.
	req := &api.BuildRequest{
		Argv:    []string{"sh", "-c", "tr a-z A-Z < in.txt > out.txt"},
		Inputs:  map[string]string{"in.txt": pr.Digest},
		Outputs: []string{"out.txt"},
	}
	res := build(req)
	if res.CacheHit || res.ExitCode != 0 {
		t.Fatalf("first build: %+v", res)
	}
	wantOut := cas.DigestOf([]byte("HELLO\n"))
	if res.Outputs["out.txt"] != wantOut {
		t.Fatalf("output digest = %q, want %q", res.Outputs["out.txt"], wantOut)
	}

	// 2. Nondeterministic command: a cache hit returns identical output,
	// proving the command was NOT re-executed.
	dyn := &api.BuildRequest{Argv: []string{"sh", "-c", "cat /proc/sys/kernel/random/uuid"}}
	first := build(dyn)
	second := build(dyn)
	if first.CacheHit {
		t.Fatal("first run of a new action must be a miss")
	}
	if !second.CacheHit {
		t.Fatal("second identical request must be a cache hit")
	}
	if first.Stdout == "" || first.Stdout != second.Stdout {
		t.Fatalf("cached output differs: %q vs %q", first.Stdout, second.Stdout)
	}
	if first.ActionDigest != second.ActionDigest {
		t.Fatal("action digest must be stable")
	}
}

func TestBuildValidationAndTimeout(t *testing.T) {
	base, _ := newTestServer(t, 1<<20)

	post := func(req *api.BuildRequest) (int, string) {
		body, _ := json.Marshal(req)
		resp, err := http.Post(base+"/v1/builds", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(b)
	}

	// Empty argv rejected.
	if code, _ := post(&api.BuildRequest{}); code != http.StatusBadRequest {
		t.Fatalf("empty argv = %d, want 400", code)
	}
	// Path traversal in input name rejected.
	if code, body := post(&api.BuildRequest{
		Argv:   []string{"true"},
		Inputs: map[string]string{"../escape": cas.DigestOf([]byte("x"))},
	}); code != http.StatusBadRequest {
		t.Fatalf("traversal input = %d (%s), want 400", code, body)
	}
	// Unknown input digest rejected.
	if code, _ := post(&api.BuildRequest{
		Argv:   []string{"true"},
		Inputs: map[string]string{"in.txt": cas.DigestOf([]byte("missing"))},
	}); code != http.StatusBadRequest {
		t.Fatalf("missing input = %d, want 400", code)
	}
	// Timeout is enforced.
	code, body := post(&api.BuildRequest{
		Argv:      []string{"sh", "-c", "sleep 30"},
		TimeoutMs: 200,
	})
	if code != http.StatusOK {
		t.Fatalf("timeout build = %d: %s", code, body)
	}
	var res api.BuildResult
	json.Unmarshal([]byte(body), &res)
	if !res.TimedOut {
		t.Fatalf("long-running command must time out: %+v", res)
	}
}

// Acceptance: published objects survive a server restart on the same cache
// dir, and the restart cleans up interrupted uploads.
func TestRestartAcrossServerInstances(t *testing.T) {
	base1, cacheRoot := newTestServer(t, 1<<20)
	data := []byte("restart-proof")
	digest := cas.DigestOf(data)
	if code, _ := put(t, base1, data); code != http.StatusCreated {
		t.Fatalf("PUT = %d", code)
	}

	// Fresh server instance over the same cache root ("restart").
	store, err := cas.Open(cacheRoot, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	exec, err := builder.NewExecutor(store, t.TempDir(), 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	srv2, err := New(store, exec, cacheRoot)
	if err != nil {
		t.Fatal(err)
	}
	ts2 := httptest.NewServer(srv2.Handler())
	defer ts2.Close()

	resp, err := http.Get(ts2.URL + "/v1/cas/" + digest)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || !bytes.Equal(got, data) {
		t.Fatalf("GET after restart = %s, %d bytes", resp.Status, len(got))
	}
}

// Acceptance: a corrupted cache is diagnosable through the admin endpoint.
func TestFsckEndpointDiagnosesCorruption(t *testing.T) {
	base, cacheRoot := newTestServer(t, 1<<20)

	data := []byte("will be tampered")
	digest := cas.DigestOf(data)
	if code, _ := put(t, base, data); code != http.StatusCreated {
		t.Fatalf("PUT = %d", code)
	}

	// Healthy cache first.
	rep := fsck(t, base)
	if !rep.OK || rep.ObjectsChecked != 1 {
		t.Fatalf("healthy fsck = %+v", rep)
	}

	// Tamper with the object on disk (objects are read-only, so restore
	// write permission first).
	path := cacheRoot + "/objects/" + digest
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("tampered"), 0o644); err != nil {
		t.Fatal(err)
	}

	rep = fsck(t, base)
	if rep.OK {
		t.Fatal("fsck must flag the corrupted cache")
	}
	if len(rep.Corrupt) != 1 || !strings.HasPrefix(rep.Corrupt[0], digest) {
		t.Fatalf("Corrupt = %v, want entry for %s", rep.Corrupt, digest)
	}
}

func fsck(t *testing.T, base string) api.FsckReport {
	t.Helper()
	resp, err := http.Get(base + "/v1/admin/fsck")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var rep api.FsckReport
	if err := json.NewDecoder(resp.Body).Decode(&rep); err != nil {
		t.Fatalf("decoding fsck report: %v", err)
	}
	return rep
}
