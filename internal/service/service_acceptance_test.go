package service_test

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"buildprovenance/internal/executor"
	"buildprovenance/internal/provenance"
	"buildprovenance/internal/service"
)

// fixture holds a fully wired, isolated service environment per test.
type fixture struct {
	t         *testing.T
	root      string
	svc       *service.Service
	log       *provenance.Log
	srcCAS    *provenance.CAS
	artCAS    *provenance.CAS
	sources   *provenance.SourceRegistry
	artifacts *provenance.ArtifactRegistry
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	root := t.TempDir()
	sourceDir := filepath.Join(root, "sources")
	artifactDir := filepath.Join(root, "artifacts")
	work := filepath.Join(root, "work")
	logPath := filepath.Join(root, "attestations.log")

	srcCAS, err := provenance.NewCAS(sourceDir)
	if err != nil {
		t.Fatal(err)
	}
	artCAS, err := provenance.NewCAS(artifactDir)
	if err != nil {
		t.Fatal(err)
	}
	sources := provenance.NewSourceRegistry(srcCAS)
	artifacts := provenance.NewArtifactRegistry(artCAS)
	key := bytesRepeat(0xAB, 32)
	attLog, err := provenance.OpenLog(logPath, key)
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
		Log: attLog, WorkRoot: work,
	})
	if err != nil {
		t.Fatal(err)
	}
	return &fixture{
		t: t, root: root, svc: svc, log: attLog, srcCAS: srcCAS, artCAS: artCAS,
		sources: sources, artifacts: artifacts,
	}
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

func bytesRepeat(b byte, n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = b
	}
	return out
}

// registerTestSources registers the shared-dependency demo graph inputs.
func (f *fixture) registerTestSources() {
	f.t.Helper()
	for _, p := range []string{"common.js", "appA.js", "appB.js", "nondet_src.py"} {
		b, err := os.ReadFile(filepath.Join(findRepoRoot(f.t), "testdata", "sources", p))
		if err != nil {
			f.t.Fatal(err)
		}
		if _, err := f.svc.RegisterSource("src/"+p, b); err != nil {
			f.t.Fatal(err)
		}
	}
}

func (f *fixture) registerTools() {
	f.t.Helper()
	tools := []provenance.ToolDefinition{
		{Name: "compile_lib", Command: []string{"bash", "compile_lib.sh"}},
		{Name: "link_app", Command: []string{"bash", "link_app.sh"}},
		{Name: "nondet", Command: []string{"bash", "nondet.sh"}},
	}
	for _, d := range tools {
		if _, err := f.svc.RegisterTool(d); err != nil {
			f.t.Fatalf("register %s: %v", d.Name, err)
		}
	}
}

// buildSharedGraph constructs:
//
//	libA = compile(common, appA)   libB = compile(common, appB)
//	binA = link(appA_src? no -> appA.js as APP, libA as LIB)
//
// To make shared dependency meaningful across outputs we use:
//
//	libA = compile_lib(COMMON=common.js, PART=appA.js)
//	libB = compile_lib(COMMON=common.js, PART=appB.js)
//	binA = link_app(APP=appA.js,        LIB=libA)
//	binB = link_app(APP=appB.js,        LIB=libB)
//
// Every downstream artifact transitively contains common.js.
func (f *fixture) buildSharedGraph() (libA, libB, binA, binB provenance.Artifact) {
	f.t.Helper()
	ctx := context.Background()

	run := func(req provenance.ActionRequest) *service.ActionResult {
		f.t.Helper()
		res, err := f.svc.ExecuteAction(ctx, req)
		if err != nil {
			f.t.Fatalf("execute %s: %v", req.Tool, err)
		}
		return res
	}
	out := func(r *service.ActionResult, slot string) provenance.Artifact {
		for _, a := range r.Artifacts {
			if a.Name == slot {
				return a
			}
		}
		f.t.Fatalf("output %s not found", slot)
		return provenance.Artifact{}
	}

	libA = out(run(provenance.ActionRequest{
		Tool: "compile_lib",
		Inputs: []provenance.Binding{
			{Slot: "COMMON", SourcePath: "src/common.js"},
			{Slot: "PART", SourcePath: "src/appA.js"},
		},
		Outputs: []string{"LIB"},
	}), "LIB")

	libB = out(run(provenance.ActionRequest{
		Tool: "compile_lib",
		Inputs: []provenance.Binding{
			{Slot: "COMMON", SourcePath: "src/common.js"},
			{Slot: "PART", SourcePath: "src/appB.js"},
		},
		Outputs: []string{"LIB"},
	}), "LIB")

	binA = out(run(provenance.ActionRequest{
		Tool: "link_app",
		Inputs: []provenance.Binding{
			{Slot: "APP", SourcePath: "src/appA.js"},
			{Slot: "LIB", ArtifactID: libA.ID},
		},
		Outputs: []string{"BIN"},
	}), "BIN")

	binB = out(run(provenance.ActionRequest{
		Tool: "link_app",
		Inputs: []provenance.Binding{
			{Slot: "APP", SourcePath: "src/appB.js"},
			{Slot: "LIB", ArtifactID: libB.ID},
		},
		Outputs: []string{"BIN"},
	}), "BIN")
	return
}

func mustIssueCode(t *testing.T, rep *service.VerifyReport, code string) {
	t.Helper()
	for _, is := range rep.Issues {
		if is.Code == code {
			return
		}
	}
	codes := make([]string, len(rep.Issues))
	for i, is := range rep.Issues {
		codes[i] = is.Code
	}
	t.Fatalf("expected issue %s, got codes %v", code, codes)
}

// ---- acceptance tests ------------------------------------------------------

func TestCompleteProvenanceVerifies(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	_, _, binA, binB := f.buildSharedGraph()

	for _, id := range []string{binA.ID, binB.ID} {
		rep, err := f.svc.Verify(id)
		if err != nil {
			t.Fatal(err)
		}
		if !rep.Complete {
			t.Fatalf("expected complete provenance for %s, issues: %+v", id, rep.Issues)
		}
		if rep.Depth != 1 {
			t.Fatalf("expected depth 1, got %d", rep.Depth)
		}
		if rep.BlobsHashed < 3 {
			t.Fatalf("expected independent re-hash of artifact+sources+upstream blobs, got %d", rep.BlobsHashed)
		}
	}
}

func TestIndependentRecomputation(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, binA, _ := f.buildSharedGraph()

	// Re-hash the artifact blob independently and confirm it matches the
	// attestation output digest (i.e. the recorded digest was not merely
	// copied from the index).
	for _, id := range []string{libA.ID, binA.ID} {
		blob, err := f.svc.ReadArtifactBytes(id)
		if err != nil {
			t.Fatal(err)
		}
		independently := provenance.DigestBytes(blob)
		rec, err := f.svc.GetRecord(id)
		if err != nil {
			t.Fatal(err)
		}
		if independently != rec.OutputDigest {
			t.Fatalf("artifact %s: independent digest %s != record outputDigest %s", id, independently, rec.OutputDigest)
		}
		// Record hash itself recomputes from canonical content.
		rh, err := provenance.RecordHash(rec)
		if err != nil {
			t.Fatal(err)
		}
		if rh != rec.RecordHash {
			t.Fatalf("record hash does not recompute for %s", id)
		}
		// Transitive: binA's upstream record hash equals libA's own.
		if id == binA.ID {
			if rec.Upstreams[libA.ID] == "" {
				t.Fatal("binA record missing libA upstream binding")
			}
			libRec, _ := f.svc.GetRecord(libA.ID)
			if rec.Upstreams[libA.ID] != libRec.RecordHash {
				t.Fatal("transitive upstream record hash mismatch")
			}
		}
	}
}

func TestSharedDependencyImpact(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, libB, binA, binB := f.buildSharedGraph()

	// common.js is the shared dependency: changing it must affect EVERY
	// downstream output of both apps.
	rep, err := f.svc.Impact("src/common.js", "")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{libA.ID: true, libB.ID: true, binA.ID: true, binB.ID: true}
	got := map[string]bool{}
	for _, id := range rep.Affected {
		got[id] = true
	}
	for id := range want {
		if !got[id] {
			t.Fatalf("common.js change must affect %s; affected=%v", id, rep.Affected)
		}
	}

	// appA.js is app-specific: must not reach into app B's outputs.
	repA, err := f.svc.Impact("src/appA.js", "")
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range repA.Affected {
		if id == libB.ID || id == binB.ID {
			t.Fatalf("appA.js must not affect appB artifacts, affected=%v", repA.Affected)
		}
	}
	if len(repA.DirectOnly) != 2 { // libA (PART slot) and binA (APP slot) consume directly
		t.Fatalf("expected 2 direct consumers of appA.js, got %v", repA.DirectOnly)
	}

	// Re-registering common.js with changed content: previously built
	// artifacts are now stale relative to the current digest.
	if _, err := f.svc.RegisterSource("src/common.js", []byte("// changed common\n")); err != nil {
		t.Fatal(err)
	}
	repStale, err := f.svc.Impact("src/common.js", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(repStale.Affected) != 0 {
		t.Fatalf("no artifact was built from the NEW digest; expected 0 exact-affected, got %v", repStale.Affected)
	}
	if len(repStale.AllDescendants) != 4 {
		t.Fatalf("all 4 artifacts descend from the logical path common.js, got %v", repStale.AllDescendants)
	}
}

func TestMissingArtifactBlobDetected(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, _, _ := f.buildSharedGraph()

	// Delete the content-addressed blob of libA out from under the index.
	blobPath := filepath.Join(f.root, "artifacts", libA.Digest.Hex()[:2], libA.Digest.Hex()[2:])
	if err := os.Remove(blobPath); err != nil {
		t.Fatal(err)
	}
	rep, err := f.svc.Verify(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Complete {
		t.Fatal("verification must fail with missing blob")
	}
	mustIssueCode(t, rep, service.CodeArtifactBlobMissing)
}

func TestTamperedArtifactBlobDetected(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, binA, _ := f.buildSharedGraph()

	// Overwrite libA's CAS blob with different bytes, keeping its path
	// (indexed digest) intact.
	blobPath := filepath.Join(f.root, "artifacts", libA.Digest.Hex()[:2], libA.Digest.Hex()[2:])
	if err := os.WriteFile(blobPath, []byte("TAMPERED CONTENT\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	rep, err := f.svc.Verify(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Complete {
		t.Fatal("verification must fail with content tampering")
	}
	mustIssueCode(t, rep, service.CodeArtifactDigestMismatch)

	// The downstream consumer must fail too, on the input digest mismatch.
	repDown, err := f.svc.Verify(binA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if repDown.Complete {
		t.Fatal("downstream verification must fail when upstream bytes are tampered")
	}
	mustIssueCode(t, repDown, service.CodeInputDigestMismatch)
}

func TestTamperedAttestationRecordDetected(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, _, _ := f.buildSharedGraph()

	rec, err := f.svc.GetRecord(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	// Forge the record in place: alter the recorded tool digest but keep a
	// "valid-looking" HMAC over the *old* hash. The independent recomputation
	// must catch the content change.
	rec.ToolDigest = provenance.Digest("sha256:" + strings.Repeat("00", 32))
	if err := f.svc.ForgeRecordForTest(rec); err != nil {
		t.Fatal(err)
	}
	rep, err := f.svc.Verify(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Complete {
		t.Fatal("forged record content must fail verification")
	}
	mustIssueCode(t, rep, service.CodeRecordHashMismatch)

	// Independently of the in-memory forgery, editing the on-disk log must
	// make a fresh service process refuse to reopen it.
	logPath := filepath.Join(f.root, "attestations.log")
	raw, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), "\"toolDigest\":\"sha256:") {
		t.Fatal("sanity: expected toolDigest field in log line")
	}
	tamperedLine := strings.Replace(string(raw), "\"recordHash\":\"", "\"recordHash\":\"00", 1)
	if err := os.WriteFile(logPath, []byte(tamperedLine), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := provenance.OpenLog(logPath, f.log.Key()); err == nil {
		t.Fatal("expected reopen to reject tampered log line")
	}
}

func TestForgedUpstreamHashDetected(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, binA, _ := f.buildSharedGraph()

	// Simulate an attacker who rewrites binA's upstream map to point at a
	// different record hash than the one libA actually attests. Because the
	// record is HMAC'd, they must also re-sign; even with a valid signature
	// over the new (forged) content, independent verification cross-checks
	// the claimed hash against libA's actual record.
	binRec, err := f.svc.GetRecord(binA.ID)
	if err != nil {
		t.Fatal(err)
	}
	forged := "deadbeef" + strings.Repeat("00", 28)
	binRec.Upstreams = map[string]string{libA.ID: forged}
	prev, err := f.log.PrevOf(binA.ID)
	if err != nil {
		t.Fatal(err)
	}
	binRec.Prev = prev
	h, err := provenance.RecordHash(binRec)
	if err != nil {
		t.Fatal(err)
	}
	binRec.RecordHash = h
	mac := hmac.New(sha256.New, f.log.Key())
	mac.Write([]byte(h))
	binRec.Sig = hex.EncodeToString(mac.Sum(nil))
	if err := f.svc.ForgeRecordForTest(binRec); err != nil {
		t.Fatal(err)
	}

	rep, err := f.svc.Verify(binA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Complete {
		t.Fatal("forged upstream hash must fail verification")
	}
	mustIssueCode(t, rep, service.CodeUpstreamHashMismatch)
}

func TestForgedCycleDetected(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, binA, _ := f.buildSharedGraph()

	// Normal execution can never produce a cycle (inputs must pre-exist).
	// Simulate a forged store where binA appears to depend on libA AND libA
	// appears to depend on binA, by rewriting libA's record upstream map.
	libRec, err := f.svc.GetRecord(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	libRec.Upstreams = map[string]string{binA.ID: f.recordHashOrDie(binA.ID)}
	h, err := provenance.RecordHash(libRec)
	if err != nil {
		t.Fatal(err)
	}
	libRec.RecordHash = h
	mac := hmac.New(sha256.New, f.log.Key())
	mac.Write([]byte(h))
	libRec.Sig = hex.EncodeToString(mac.Sum(nil))
	if err := f.svc.ForgeRecordForTest(libRec); err != nil {
		t.Fatal(err)
	}

	if cyc, err := f.findCycleExported(binA.ID); err != nil {
		t.Fatal(err)
	} else if len(cyc) < 3 {
		t.Fatalf("expected to find forged cycle, got %v", cyc)
	}
	rep, err := f.svc.Verify(binA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Complete {
		t.Fatal("forged cycle must fail verification")
	}
	mustIssueCode(t, rep, service.CodeCycleDetected)

	// Execute must also refuse to build atop a cyclic graph.
	_, err = f.svc.ExecuteAction(context.Background(), provenance.ActionRequest{
		Tool: "link_app",
		Inputs: []provenance.Binding{
			{Slot: "APP", SourcePath: "src/appA.js"},
			{Slot: "LIB", ArtifactID: binA.ID},
		},
		Outputs: []string{"BIN"},
	})
	if err == nil || !strings.Contains(err.Error(), "cycle") {
		t.Fatalf("execute on cyclic graph must be refused, got %v", err)
	}
}

func TestToolDigestDriftDetected(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, _, _ := f.buildSharedGraph()

	// Modify the fixture script on disk after the build.
	script := filepath.Join(findRepoRoot(t), "fixtures", "compile_lib.sh")
	orig, err := os.ReadFile(script)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.WriteFile(script, orig, 0o755) })
	tampered := append(append([]byte{}, orig...), []byte("\n# attacker-modified fixture line\n")...)
	if err := os.WriteFile(script, tampered, 0o755); err != nil {
		t.Fatal(err)
	}

	rep, err := f.svc.Verify(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Complete {
		t.Fatal("changed fixture script must invalidate the tool digest")
	}
	mustIssueCode(t, rep, service.CodeToolDigestMismatch)

	// And re-registration under the same name with a different digest is
	// rejected (tools are immutable).
	_, err = f.svc.RegisterTool(provenance.ToolDefinition{Name: "compile_lib", Command: []string{"bash", "compile_lib.sh"}})
	if err == nil || !strings.Contains(err.Error(), "different digest") {
		t.Fatalf("immutable tool re-registration must fail, got %v", err)
	}
}

func TestReproducibleVersusNonDeterministic(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	_, _, binA, _ := f.buildSharedGraph()

	// Deterministic tooling: complete provenance AND reproducible output.
	rep, err := f.svc.Reproduce(context.Background(), binA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.ProvenanceComplete {
		t.Fatalf("provenance should be complete: %+v", rep.Notes)
	}
	if !rep.Reproducible {
		t.Fatalf("deterministic build should reproduce, outputs=%+v notes=%v", rep.Outputs, rep.Notes)
	}
	for _, o := range rep.Outputs {
		if !o.Match || o.RecordedDigest != o.ReproducedDigest {
			t.Fatalf("output %s digests must match", o.Slot)
		}
	}

	// Non-deterministic tool: provenance is complete (every input/tool fact
	// truthful), yet rerunning produces different bytes.
	res, err := f.svc.ExecuteAction(context.Background(), provenance.ActionRequest{
		Tool: "nondet",
		Inputs: []provenance.Binding{
			{Slot: "SRC", SourcePath: "src/nondet_src.py"},
		},
		Outputs: []string{"BIN"},
	})
	if err != nil {
		t.Fatal(err)
	}
	nondet := res.Artifacts[0]
	// Verify first: complete.
	vrep, err := f.svc.Verify(nondet.ID)
	if err != nil {
		t.Fatal(err)
	}
	if !vrep.Complete {
		t.Fatalf("nondet build should still have complete provenance, got %+v", vrep.Issues)
	}
	time.Sleep(15 * time.Millisecond)
	rrep, err := f.svc.Reproduce(context.Background(), nondet.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rrep.Reproducible {
		t.Fatal("time-dependent build must NOT be reproducible even with complete provenance")
	}
	if !rrep.ProvenanceComplete {
		t.Fatal("provenance completeness is independent of reproducibility")
	}
	for _, o := range rrep.Outputs {
		if o.Match {
			t.Fatalf("nondet output %s unexpectedly matched", o.Slot)
		}
	}
}

func TestDisallowedCommandsRejected(t *testing.T) {
	f := newFixture(t)
	bad := []provenance.ToolDefinition{
		{Name: "x1", Command: []string{"/bin/cat", "/etc/passwd"}},
		{Name: "x2", Command: []string{"bash", "../../../etc/passwd"}},
		{Name: "x3", Command: []string{"/bin/sh", "-c", "echo hi"}},
	}
	for _, d := range bad {
		if _, err := f.svc.RegisterTool(d); err == nil {
			t.Fatalf("disallowed command %v was accepted", d.Command)
		}
	}
}

func TestWorkDirIsSeparateFromCaches(t *testing.T) {
	f := newFixture(t)
	f.registerTestSources()
	f.registerTools()
	libA, _, _, _ := f.buildSharedGraph()

	// Artifact bytes live only in the artifact CAS, never in the work tree.
	blob, err := f.svc.ReadArtifactBytes(libA.ID)
	if err != nil {
		t.Fatal(err)
	}
	if string(blob) == "" {
		t.Fatal("artifact blob empty")
	}
	entries, err := os.ReadDir(filepath.Join(f.root, "work"))
	if err != nil {
		t.Fatal(err)
	}
	// After successful builds the throwaway work/stage directories are gone.
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "build-") || strings.HasPrefix(e.Name(), "stage-") || strings.HasPrefix(e.Name(), "repro-stage-") {
			t.Fatalf("work dir %s was not cleaned up", e.Name())
		}
	}
	// Source and artifact CAS are separate directories.
	if sameDir(t, filepath.Join(f.root, "sources"), filepath.Join(f.root, "artifacts")) {
		t.Fatal("source and artifact caches must be separate")
	}
}

func sameDir(t *testing.T, a, b string) bool {
	t.Helper()
	ai, err1 := os.Stat(a)
	bi, err2 := os.Stat(b)
	return err1 == nil && err2 == nil && os.SameFile(ai, bi)
}

// ---- whitebox helpers ------------------------------------------------------

func (f *fixture) recordHashOrDie(id string) string {
	r, err := f.svc.GetRecord(id)
	if err != nil {
		f.t.Fatal(err)
	}
	return r.RecordHash
}

// findCycleExported exercises cycle detection through the public Verify path
// indirectly; here we inspect via a tiny reimplementation-free call by
// re-using impact traversal guarantees: instead call Verify and read issue.
func (f *fixture) findCycleExported(id string) ([]string, error) {
	// Use the provenance tree endpoint-equivalent: cycle is asserted via
	// Verify; provide a simple DFS here using log records.
	recs := f.svc.ListRecords()
	m := map[string]provenance.Record{}
	for _, r := range recs {
		m[r.ArtifactID] = r
	}
	color := map[string]int{}
	var stack []string
	var dfs func(string) []string
	dfs = func(x string) []string {
		color[x] = 1
		stack = append(stack, x)
		ups := m[x].Upstreams
		var keys []string
		for k := range ups {
			keys = append(keys, k)
		}
		for _, u := range keys {
			switch color[u] {
			case 1:
				for i, v := range stack {
					if v == u {
						return append(append([]string{}, stack[i:]...), u)
					}
				}
			case 0:
				if c := dfs(u); c != nil {
					return c
				}
			}
		}
		stack = stack[:len(stack)-1]
		color[x] = 2
		return nil
	}
	return dfs(id), nil
}
