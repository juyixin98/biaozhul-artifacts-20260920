package builder

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"buildcache/internal/store"
)

// ---- fixtures -------------------------------------------------------------

func newTestBuilder(t *testing.T) (*Builder, *store.Store, string) {
	t.Helper()
	dataDir := t.TempDir()
	sourceRoot := t.TempDir()
	st, err := store.Open(context.Background(), filepath.Join(dataDir, "cache.db"), dataDir)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	b, err := New(st, dataDir, sourceRoot)
	if err != nil {
		t.Fatalf("new builder: %v", err)
	}
	return b, st, dataDir
}

var (
	goMod  = []byte("module demo\n\ngo 1.22\n")
	mainGo = []byte("package main\n\nimport \"fmt\"\n\n" +
		"func main() { fmt.Printf(\"demo:%s\\n\", flavor()) }\n")
	proGo  = []byte("//go:build pro\n\npackage main\n\nfunc flavor() string { return \"pro\" }\n")
	liteGo = []byte("//go:build !pro\n\npackage main\n\nfunc flavor() string { return \"lite\" }\n")
)

// goBuildRequest builds the demo package. declaredGOFLAGS is the declared
// GOFLAGS environment variable ("" means not declared at all).
func goBuildRequest(declaredGOFLAGS string, mutate func(map[string]string)) *Request {
	env := map[string]string{}
	if declaredGOFLAGS != "__unset__" {
		env["GOFLAGS"] = declaredGOFLAGS
	}
	if mutate != nil {
		mutate(env)
	}
	sources := []SourceInput{
		{Path: "go.mod", Inline: goMod},
		{Path: "main.go", Inline: mainGo},
		{Path: "flavor_pro.go", Inline: proGo},
		{Path: "flavor_lite.go", Inline: liteGo},
	}
	return &Request{
		ToolchainName:   "go",
		VersionCommand:  []string{"go", "version"},
		CommandTemplate: []string{"go", "build", "-buildvcs=false", "{args}", "-o", "{artifact}", "."},
		Args:            nil,
		Target:          map[string]string{"GOOS": "linux", "GOARCH": "amd64"},
		Env:             env,
		Sources:         sources,
		Artifact:        "app",
		TimeoutSeconds:  120,
	}
}

// ---- tests ----------------------------------------------------------------

func TestRealCompile_MissHitAndKeyInvalidation(t *testing.T) {
	b, _, _ := newTestBuilder(t)
	ctx := context.Background()

	// 1) Cold build (lite: GOFLAGS not declared).
	lite := goBuildRequest("__unset__", nil)
	o1, err := b.Build(ctx, lite, time.Minute)
	if err != nil {
		t.Fatalf("first build: %v", err)
	}
	if o1.Hit || o1.Status != store.StatusSucceeded {
		t.Fatalf("first build should be a fresh success, got hit=%v status=%s err=%s",
			o1.Hit, o1.Status, o1.Error)
	}
	rel1 := blobRel(o1.ArtifactSHA)
	if got := runBlobAt(t, filepath.Join(b.blobsDir, rel1)); got != "demo:lite" {
		t.Fatalf("lite artifact ran as %q want demo:lite", got)
	}

	// 2) Identical request -> real cache hit, no rebuild.
	o2, err := b.Build(ctx, goBuildRequest("__unset__", nil), time.Minute)
	if err != nil {
		t.Fatalf("second build: %v", err)
	}
	if !o2.Hit || o2.Key != o1.Key || o2.ArtifactSHA != o1.ArtifactSHA {
		t.Fatalf("expected verified hit on identical request: %+v", o2)
	}

	// 3) Declare GOFLAGS=-tags pro: declared env participates in the key,
	//    so this is a different build producing a genuinely different binary.
	pro := goBuildRequest("-tags=pro", nil)
	o3, err := b.Build(ctx, pro, time.Minute)
	if err != nil {
		t.Fatalf("pro build: %v", err)
	}
	if o3.Hit || o3.Key == o1.Key {
		t.Fatalf("pro build must miss with a different key: hit=%v keysEqual=%v",
			o3.Hit, o3.Key == o1.Key)
	}
	if got := runBlobAt(t, filepath.Join(b.blobsDir, blobRel(o3.ArtifactSHA))); got != "demo:pro" {
		t.Fatalf("pro artifact ran as %q want demo:pro", got)
	}

	// 4) Edit a source: key changes again, another miss.
	editedMain := append([]byte(nil), mainGo...)
	editedMain = []byte(strings.ReplaceAll(string(editedMain), "demo:", "v2:"))
	editReq := goBuildRequest("__unset__", nil)
	editReq.Sources[1].Inline = editedMain
	o4, err := b.Build(ctx, editReq, time.Minute)
	if err != nil {
		t.Fatalf("edited build: %v", err)
	}
	if o4.Hit || o4.Key == o1.Key {
		t.Fatalf("edited source must change the key and miss")
	}
	if got := runBlobAt(t, filepath.Join(b.blobsDir, blobRel(o4.ArtifactSHA))); got != "v2:lite" {
		t.Fatalf("edited artifact = %q want v2:lite", got)
	}

	// 5) Back to the original material: the original artifact is still
	//    cached and verifies -> hit, even though other builds intervened.
	o5, err := b.Build(ctx, goBuildRequest("__unset__", nil), time.Minute)
	if err != nil {
		t.Fatalf("revisit build: %v", err)
	}
	if !o5.Hit || o5.Key != o1.Key {
		t.Fatalf("original material should hit original entry, got %+v", o5)
	}
}

func TestOmittedEnvCannotReuseStaleResult(t *testing.T) {
	b, _, _ := newTestBuilder(t)
	ctx := context.Background()

	// Build under declared GOFLAGS=-tags=pro.
	pro := goBuildRequest("-tags=pro", nil)
	oPro, err := b.Build(ctx, pro, time.Minute)
	if err != nil {
		t.Fatalf("pro build: %v", err)
	}
	if got := runBlobAt(t, filepath.Join(b.blobsDir, blobRel(oPro.ArtifactSHA))); got != "demo:pro" {
		t.Fatalf("want demo:pro, got %q", got)
	}

	// Now the same request *except the env declaration is omitted*. The key
	// must differ (so no stale pro result is reused) and the controlled child
	// environment does not inherit GOFLAGS, so the real build is lite.
	omitted := goBuildRequest("__unset__", nil)
	oOmit, err := b.Build(ctx, omitted, time.Minute)
	if err != nil {
		t.Fatalf("omitted-env build: %v", err)
	}
	if oOmit.Key == oPro.Key {
		t.Fatal("omitting a declared env var must change the key")
	}
	if oOmit.Hit {
		t.Fatal("omitted-env build must not hit the pro entry")
	}
	if got := runBlobAt(t, filepath.Join(b.blobsDir, blobRel(oOmit.ArtifactSHA))); got != "demo:lite" {
		t.Fatalf("omitted env must build lite, got %q", got)
	}

	// And vice versa: declaring a previously-undeclared var also changes key.
	added := goBuildRequest("__unset__", func(env map[string]string) { env["CGO_ENABLED"] = "0" })
	oAdd, err := b.Build(ctx, added, time.Minute)
	if err != nil {
		t.Fatalf("added-env build: %v", err)
	}
	if oAdd.Key == oOmit.Key {
		t.Fatal("adding a declared env var must change the key")
	}
}

func TestConcurrentSameKeySinglePublisher(t *testing.T) {
	b, st, _ := newTestBuilder(t)
	ctx := context.Background()

	slowReq := func(tag string) *Request {
		return &Request{
			ToolchainName:  "coreutils",
			VersionCommand: []string{"sh", "-c", "echo posix-sh 1.0"},
			CommandTemplate: []string{
				"sh", "-c", "sleep 2; cp /bin/true \"$1\"", tag, "{artifact}",
			},
			Target:         map[string]string{"X": "y"},
			Env:            map[string]string{},
			Sources:        []SourceInput{{Path: "note-" + tag + ".txt", Inline: []byte("slow build " + tag)}},
			Artifact:       "true-" + tag + ".bin",
			TimeoutSeconds: 30,
		}
	}

	// A non-publisher that finds the lease already held and exhausts its
	// wait deadline gets a clean conflict rather than starting a second build.
	conflictReq := slowReq("conflict")
	m, _, err := b.ComputeMaterial(ctx, conflictReq)
	if err != nil {
		t.Fatal(err)
	}
	_, conflictKey, err := KeyOnly(m)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := st.Claim(ctx, conflictKey); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	if _, err := b.Build(ctx, conflictReq, 400*time.Millisecond); err != ErrConflict {
		t.Fatalf("want ErrConflict, got %v", err)
	}
	if d := time.Since(start); d < 350*time.Millisecond {
		t.Fatalf("conflict returned early after %v", d)
	}

	// Simultaneous race on a fresh key with a genuinely slow build: exactly
	// one goroutine may publish; the rest block on the lease and take hits.
	req := slowReq("race")
	const n = 5
	var wg sync.WaitGroup
	results := make([]*Outcome, n)
	errs := make([]error, n)
	barrier := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-barrier
			results[i], errs[i] = b.Build(ctx, req, 30*time.Second)
		}(i)
	}
	close(barrier)
	wg.Wait()

	misses, hits := 0, 0
	var sha string
	for i := 0; i < n; i++ {
		if errs[i] != nil {
			t.Fatalf("racer %d: %v", i, errs[i])
		}
		if results[i].Status != store.StatusSucceeded {
			t.Fatalf("racer %d status=%s err=%s", i, results[i].Status, results[i].Error)
		}
		if results[i].Hit {
			hits++
		} else {
			misses++
		}
		if sha == "" {
			sha = results[i].ArtifactSHA
		} else if results[i].ArtifactSHA != sha {
			t.Fatal("racers observed different artifacts")
		}
	}
	if misses != 1 || hits != n-1 {
		t.Fatalf("want exactly 1 publisher and %d hits, got misses=%d hits=%d", n-1, misses, hits)
	}
	// Content-addressable store holds exactly one physical blob for the key.
	matches, _ := filepath.Glob(filepath.Join(b.blobsDir, "??", sha))
	if len(matches) != 1 {
		t.Fatalf("expected exactly one stored blob, found %d", len(matches))
	}
}

func TestArtifactLossAndTamperAreQuarantined(t *testing.T) {
	b, st, dataDir := newTestBuilder(t)
	ctx := context.Background()
	req := shFileRequest("greeting", "hello artifact")
	o, err := b.Build(ctx, req, time.Minute)
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if o.Hit {
		t.Fatal("first build must miss")
	}
	rel := blobRel(o.ArtifactSHA)
	blob := filepath.Join(b.blobsDir, rel)

	// Tamper with the blob (same length to isolate digest, not size, as the
	// failure signal): next read must isolate it and rebuild.
	tampered := strings.Repeat("Z", int(o.ArtifactSize))
	if err := os.WriteFile(blob, []byte(tampered), 0o755); err != nil {
		t.Fatal(err)
	}
	o2, err := b.Build(ctx, shFileRequest("greeting", "hello artifact"), time.Minute)
	if err != nil {
		t.Fatalf("rebuild after tamper: %v", err)
	}
	_ = o2 // fresh publish after quarantine; not required to be marked Hit
	evs, err := b.Events(ctx, o.Key)
	if err != nil || len(evs) != 1 {
		t.Fatalf("quarantine events = %v, %v; want 1", evs, err)
	}
	switch evs[0].Reason {
	case "hash_mismatch", "size_mismatch":
	default:
		t.Fatalf("unexpected event reason: %+v", evs[0])
	}
	if evs[0].ObservedSHA == "" {
		t.Fatalf("event missing observed sha: %+v", evs[0])
	}
	// The corrupt bytes were physically moved into the quarantine directory.
	entries, _ := os.ReadDir(filepath.Join(dataDir, "quarantine"))
	if len(entries) != 1 {
		t.Fatalf("quarantine dir entries=%d want 1", len(entries))
	}
	e, _ := st.Get(ctx, o.Key)
	if e.Status != store.StatusSucceeded || e.ArtifactSHA != o.ArtifactSHA {
		t.Fatalf("entry not healed: %+v", e)
	}

	// Now delete the blob entirely: missing-artifact quarantine + rebuild.
	if err := os.Remove(blob); err != nil {
		t.Fatal(err)
	}
	rc, ent, q, err := b.ArtifactReader(ctx, o.Key)
	if err != nil {
		t.Fatalf("artifact read: %v", err)
	}
	if rc != nil {
		rc.Close()
	}
	if q == nil || q.Reason != "artifact_missing" {
		t.Fatalf("expected artifact_missing quarantine, got %+v", q)
	}
	if ent.Status != store.StatusQuarantined {
		t.Fatalf("entry status=%s want quarantined", ent.Status)
	}
	o3, err := b.Build(ctx, shFileRequest("greeting", "hello artifact"), time.Minute)
	if err != nil {
		t.Fatalf("rebuild after loss: %v", err)
	}
	if o3.Status != store.StatusSucceeded {
		t.Fatalf("status after rebuild = %s err=%s", o3.Status, o3.Error)
	}
}

func TestFailedBuildNeverCachedAsSuccess(t *testing.T) {
	b, st, _ := newTestBuilder(t)
	ctx := context.Background()

	// Compiler exits non-zero.
	failReq := &Request{
		ToolchainName:  "shell",
		VersionCommand: []string{"sh", "-c", "echo posix-sh 1.0"},
		CommandTemplate: []string{
			"sh", "-c", "echo diagnostic >&2; exit 3", "fail",
		},
		Target:         map[string]string{},
		Env:            map[string]string{},
		Sources:        []SourceInput{{Path: "s.txt", Inline: []byte("x")}},
		Artifact:       "out",
		TimeoutSeconds: 10,
	}
	o, err := b.Build(ctx, failReq, time.Minute)
	if err != nil {
		t.Fatalf("build returned transport error: %v", err)
	}
	if o.Hit || o.Status != store.StatusFailed || o.ExitCode != 3 {
		t.Fatalf("want failed/exit3, got %+v", o)
	}
	if !strings.Contains(o.Stderr, "diagnostic") {
		t.Fatalf("stderr not captured: %q", o.Stderr)
	}
	e, _ := st.Get(ctx, o.Key)
	if e.Status != store.StatusFailed || e.ArtifactSHA != "" || e.ArtifactPath != "" {
		t.Fatalf("failure row must not carry artifact fields: %+v", e)
	}

	// Same request again: still a miss and still failed (never cached success).
	o2, err := b.Build(ctx, failReq, time.Minute)
	if err != nil {
		t.Fatalf("second build: %v", err)
	}
	if o2.Hit || o2.Status != store.StatusFailed {
		t.Fatalf("second failed build must not hit: %+v", o2)
	}
	e2, _ := st.Get(ctx, o.Key)
	if e2.Attempt != 2 {
		t.Fatalf("attempt=%d want 2", e2.Attempt)
	}

	// Exit 0 but no artifact produced is also a failure, not a success.
	noArt := &Request{
		ToolchainName:  "shell",
		VersionCommand: []string{"sh", "-c", "echo posix-sh 1.0"},
		CommandTemplate: []string{
			"sh", "-c", "exit 0", "noop",
		},
		Target:   map[string]string{},
		Env:      map[string]string{},
		Sources:  []SourceInput{{Path: "s.txt", Inline: []byte("y")}},
		Artifact: "missing-out",
	}
	o3, err := b.Build(ctx, noArt, time.Minute)
	if err != nil {
		t.Fatalf("no-artifact build: %v", err)
	}
	if o3.Hit || o3.Status != store.StatusFailed {
		t.Fatalf("exit-0-without-artifact must be failed, got %+v", o3)
	}
}

func TestRestartKeepsVerifiedHitsAndRecoversLeases(t *testing.T) {
	dataDir := t.TempDir()
	sourceRoot := t.TempDir()

	open := func() (*Builder, *store.Store) {
		st, err := store.Open(context.Background(), filepath.Join(dataDir, "cache.db"), dataDir)
		if err != nil {
			t.Fatal(err)
		}
		b, err := New(st, dataDir, sourceRoot)
		if err != nil {
			t.Fatal(err)
		}
		return b, st
	}

	b1, st1 := open()
	req := shFileRequest("persist", "durable bytes")
	o1, err := b1.Build(context.Background(), req, time.Minute)
	if err != nil {
		t.Fatalf("first build: %v", err)
	}
	if err := st1.Close(); err != nil {
		t.Fatal(err)
	}

	// Simulate process restart: boot recovery then re-open.
	stRec, err := store.Open(context.Background(), filepath.Join(dataDir, "cache.db"), dataDir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stRec.ResetStaleLeases(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := stRec.Close(); err != nil {
		t.Fatal(err)
	}

	b2, st2 := open()
	defer st2.Close()
	o2, err := b2.Build(context.Background(), shFileRequest("persist", "durable bytes"), time.Minute)
	if err != nil {
		t.Fatalf("build after restart: %v", err)
	}
	if !o2.Hit || o2.Key != o1.Key || o2.ArtifactSHA != o1.ArtifactSHA {
		t.Fatalf("restart must preserve verified hit: %+v", o2)
	}

	// A lease held when the process dies is recovered as interrupted and a
	// new publisher takes over after boot recovery.
	m, _, _ := b2.ComputeMaterial(context.Background(), req)
	_, k, _ := KeyOnly(m)
	if _, _, err := st2.Claim(context.Background(), k); err != nil {
		t.Fatal(err)
	}
	if err := st2.Close(); err != nil {
		t.Fatal(err)
	}
	st3, err := store.Open(context.Background(), filepath.Join(dataDir, "cache.db"), dataDir)
	if err != nil {
		t.Fatal(err)
	}
	n, _ := st3.ResetStaleLeases(context.Background())
	if n != 1 {
		t.Fatalf("recovered leases=%d want 1", n)
	}
	b3, err := New(st3, dataDir, sourceRoot)
	if err != nil {
		t.Fatal(err)
	}
	o3, err := b3.Build(context.Background(), shFileRequest("persist", "durable bytes"), time.Minute)
	if err != nil {
		t.Fatalf("post-recovery build: %v", err)
	}
	if o3.Status != store.StatusSucceeded {
		t.Fatalf("status=%s err=%s", o3.Status, o3.Error)
	}
}

func TestSourceManifestDistinguishesMissingAndEmpty(t *testing.T) {
	b, _, _ := newTestBuilder(t)
	ctx := context.Background()

	// Present but zero-byte optional file (inline empty).
	presentReq := shFileRequest("manifest", "v")
	presentReq.Sources = append(presentReq.Sources, SourceInput{Path: "opt.cfg", Inline: []byte{}})
	m1, _, err := b.ComputeMaterial(ctx, presentReq)
	if err != nil {
		t.Fatal(err)
	}
	var foundPresent bool
	for _, s := range m1.Sources {
		if s.Path == "opt.cfg" {
			if !s.Present || s.Digest != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
				t.Fatalf("empty file manifest wrong: %+v", s)
			}
			foundPresent = true
		}
	}
	if !foundPresent {
		t.Fatal("manifest missing opt.cfg")
	}

	// Absent optional file: reading from the (empty) source root records it
	// as missing rather than erroring.
	missingReq := shFileRequest("manifest", "v")
	missingReq.Sources = append(missingReq.Sources, SourceInput{Path: "opt.cfg", Optional: true})
	m2, _, err := b.ComputeMaterial(ctx, missingReq)
	if err != nil {
		t.Fatalf("optional missing file must be allowed: %v", err)
	}
	var foundAbsent bool
	for _, s := range m2.Sources {
		if s.Path == "opt.cfg" {
			if s.Present || s.Digest != "" {
				t.Fatalf("missing file manifest wrong: %+v", s)
			}
			foundAbsent = true
		}
	}
	if !foundAbsent {
		t.Fatal("manifest missing opt.cfg")
	}

	_, k1, _ := KeyOnly(m1)
	_, k2, _ := KeyOnly(m2)
	if k1 == k2 {
		t.Fatal("empty file and missing file must yield different keys")
	}
}

// shFileRequest builds a deterministic artifact: a small shell program that
// writes content to the output path. The script name/argv are constant so the
// command itself stays identical across rebuilds.
func shFileRequest(label, content string) *Request {
	return &Request{
		ToolchainName:  "shell",
		VersionCommand: []string{"sh", "-c", "echo posix-sh 1.0"},
		CommandTemplate: []string{
			"sh", "-c", "printf '%s' \"$2\" > \"$1\"; chmod +x \"$1\"", label, "{artifact}", "{args}",
		},
		Args:           []string{content},
		Target:         map[string]string{},
		Env:            map[string]string{},
		Sources:        []SourceInput{{Path: "input-" + label + ".txt", Inline: []byte(content)}},
		Artifact:       "out-" + label + ".bin",
		TimeoutSeconds: 30,
	}
}

func blobRel(sha string) string { return sha[:2] + "/" + sha }
