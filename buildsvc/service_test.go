package buildsvc

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"modelcache/cacheapi"
	"modelcache/client"
	"modelcache/digest"
	"modelcache/store"

	"net/http/httptest"
)

// startStack wires a real cache server + build service using the actual
// fixtures directory (this file lives in buildsvc/, one level below repo root).
func startStack(t *testing.T, maxSize int64) (*httptest.Server, *Service, *client.Client, string) {
	t.Helper()
	goMod, err := filepath.Abs(filepath.Join("..", "go.mod"))
	if err != nil {
		t.Fatal(err)
	}
	repoRoot := filepath.Dir(goMod)
	manifestPath := filepath.Join(repoRoot, "fixtures", "manifest.json")

	cacheRoot := t.TempDir()
	st, err := store.New(store.Options{Root: cacheRoot, MaxObjectSize: maxSize})
	if err != nil {
		t.Fatal(err)
	}
	cacheTS := httptest.NewServer(cacheapi.NewServer(st, nil).Handler())
	t.Cleanup(cacheTS.Close)

	manifest, err := LoadManifest(manifestPath)
	if err != nil {
		t.Fatalf("load manifest: %v", err)
	}
	// Fixture scripts must have been resolved to absolute paths.
	if got := manifest.Fixtures[0].Args[0]; !filepath.IsAbs(got) {
		t.Fatalf("fixture arg not expanded to absolute path: %q", got)
	}

	workRoot := t.TempDir()
	artifactRoot := t.TempDir()
	cl := client.New(client.Options{BaseURL: cacheTS.URL})
	svc, err := New(Options{
		WorkDir:      workRoot,
		ArtifactDir:  artifactRoot,
		Manifest:     manifest,
		Cache:        cl,
		DefaultLimit: 20 * time.Second,
		MaxLimit:     30 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(svc.Close)
	return cacheTS, svc, cl, artifactRoot
}

func TestSuccessfulFixtureCachesArtifact(t *testing.T) {
	_, svc, cl, artifactRoot := startStack(t, 8<<20)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	j, err := svc.StartBuild("make-model", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := j.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	v := j.Snapshot()
	if v.Status != StatusSucceeded {
		t.Fatalf("status=%s err=%s logs=%s", v.Status, v.Error, v.LogsTail)
	}
	if len(v.Artifacts) != 1 || v.Artifacts[0].Name != "model.bin" || v.Artifacts[0].Size != 4096 {
		t.Fatalf("artifacts = %+v", v.Artifacts)
	}
	dgst := v.Artifacts[0].Digest

	// Artifact is retrievable from the cache and verifies.
	rc, size, err := cl.Get(ctx, dgst)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, size)
	if _, err := readFull(rc, buf); err != nil {
		t.Fatal(err)
	}
	rc.Close()
	if digest.NewSHA256(buf) != dgst {
		t.Fatal("cached artifact digest mismatch")
	}
	// Local copy in the separate artifact dir.
	if _, err := os.Stat(filepath.Join(artifactRoot, dgst.Hex())); err != nil {
		t.Fatalf("artifact dir copy missing: %v", err)
	}
	// Work dir is not the cache dir (they are distinct temp roots by stack).
	if filepath.Dir(v.WorkDir) == artifactRoot {
		t.Fatal("work dir and artifact dir must differ")
	}
}

func TestSlowFixtureProducesDeterministicArtifact(t *testing.T) {
	_, svc, cl, _ := startStack(t, 8<<20)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	j, err := svc.StartBuild("slow-model", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := j.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	v := j.Snapshot()
	if v.Status != StatusSucceeded {
		t.Fatalf("status=%s err=%s", v.Status, v.Error)
	}
	if v.Artifacts[0].Size != 1048576 {
		t.Fatalf("size=%d want 1048576", v.Artifacts[0].Size)
	}
	// Must equal the documented 64-byte pattern repeated to 1 MiB.
	pattern := []byte("modelcache-local-model-fixture-v0001----0123456789abcdef!!")
	want := make([]byte, 0, 1048576)
	for len(want) < 1048576 {
		want = append(want, pattern...)
	}
	want = want[:1048576]
	wantDgst := digest.NewSHA256(want)
	if v.Artifacts[0].Digest != wantDgst {
		t.Fatalf("slow-model digest = %s, want %s", v.Artifacts[0].Digest, wantDgst)
	}
	if _, err := cl.Stat(ctx, wantDgst); err != nil {
		t.Fatalf("slow artifact not cached: %v", err)
	}
}

func TestFailedFixtureReportedNotCached(t *testing.T) {
	_, svc, cl, _ := startStack(t, 8<<20)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	j, err := svc.StartBuild("fail-fixture", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := j.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	v := j.Snapshot()
	if v.Status != StatusFailed || v.ExitCode == nil || *v.ExitCode != 7 {
		t.Fatalf("status=%s exit=%v err=%s", v.Status, v.ExitCode, v.Error)
	}
	if len(v.Artifacts) != 0 {
		t.Fatalf("failed build must not publish artifacts: %+v", v.Artifacts)
	}
	blobs, err := cl.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(blobs) != 0 {
		t.Fatalf("cache must stay empty after failed build, got %+v", blobs)
	}
}

func TestUnknownFixtureRejected(t *testing.T) {
	_, svc, _, _ := startStack(t, 8<<20)
	// A command-line-looking name must never be executable.
	if _, err := svc.StartBuild("rm -rf /; echo pwned", nil); err == nil {
		t.Fatal("injected command name accepted")
	}
	if _, err := svc.StartBuild("bash", nil); err == nil {
		t.Fatal("arbitrary binary name accepted as fixture")
	}
	if _, err := svc.StartBuild("../bin/cacheserver", nil); err == nil {
		t.Fatal("path-traversal fixture accepted")
	}
}

func TestManifestRejectsUnsafeOutputsAndCommands(t *testing.T) {
	bad := []string{
		`{"fixtures":[{"name":"x","command":"sh -c","outputs":["a"]}]}`,
		`{"fixtures":[{"name":"x","command":"sh","outputs":["../escape"]}]}`,
		`{"fixtures":[{"name":"x","command":"sh","outputs":["/abs/path"]}]}`,
		`{"fixtures":[{"name":"BAD NAME","command":"sh","outputs":["a"]}]}`,
	}
	for _, body := range bad {
		dir := t.TempDir()
		p := filepath.Join(dir, "m.json")
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadManifest(p); err == nil {
			t.Fatalf("manifest accepted: %s", body)
		}
	}
}

func TestFixtureJSONRoundTrip(t *testing.T) {
	var f Fixture
	if err := json.Unmarshal([]byte(`{"name":"n","command":"c","args":[],"outputs":["o"],"timeout":"3s"}`), &f); err != nil {
		t.Fatal(err)
	}
	if f.Timeout.Std() != 3*time.Second {
		t.Fatalf("timeout=%v", f.Timeout)
	}
}

func readFull(r interface{ Read([]byte) (int, error) }, buf []byte) (int, error) {
	total := 0
	for total < len(buf) {
		n, err := r.Read(buf[total:])
		total += n
		if err != nil && total < len(buf) {
			return total, err
		}
	}
	return total, nil
}
