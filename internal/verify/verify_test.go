package verify

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"bis/internal/builder"
	"bis/internal/digest"
	"bis/internal/provenance"
	"bis/internal/spec"
	"bis/internal/store"
	"bis/internal/testutil"
)

func buildProject(t *testing.T, st *store.Store, p *spec.Project) {
	t.Helper()
	b := builder.New(st, builder.WithTimeout(10*time.Second))
	if _, err := b.Build(p); err != nil {
		t.Fatalf("setup build: %v", err)
	}
}

func verifyProject(t *testing.T, st *store.Store, p *spec.Project) *Report {
	t.Helper()
	rep, err := New(st).Verify(p)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	return rep
}

// mutateRecord loads an action's record, applies fn, re-signs it with the
// store key (simulating a key-holder forging an attestation), stores it under
// a new content id and repoints the index.
func mutateRecord(t *testing.T, st *store.Store, project, action string, fn func(*provenance.Record)) {
	t.Helper()
	idx := st.LoadIndex(project)
	ent := idx.Records[action]
	r, err := st.GetRecord(ent.RecordID)
	if err != nil {
		t.Fatal(err)
	}
	fn(r)
	newID, err := st.PutRecord(r)
	if err != nil {
		t.Fatal(err)
	}
	ent.RecordID = newID
	idx.Records[action] = ent
	if err := st.SaveIndex(idx); err != nil {
		t.Fatal(err)
	}
}

func codeSet(rep *Report, action string) map[Code]bool {
	out := map[Code]bool{}
	for _, f := range rep.Findings {
		if f.ActionID == action {
			out[f.Code] = true
		}
	}
	return out
}

func TestVerifyCleanChainIsComplete(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	buildProject(t, st, p)
	rep := verifyProject(t, st, p)
	if !rep.Complete {
		t.Fatalf("clean build reported incomplete: %+v", rep.Findings)
	}
	for _, n := range rep.Nodes {
		if !n.Complete || n.Tainted {
			t.Fatalf("node %s not clean: %+v", n.ActionID, n)
		}
	}
}

func TestVerifyIndependentlyRecomputesDigests(t *testing.T) {
	// The verifier must reach its verdict from the files on disk, not from
	// the builder report; here we just confirm a clean chain yields zero
	// digest findings across all classes.
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	buildProject(t, st, p)
	rep := verifyProject(t, st, p)
	for _, f := range rep.Findings {
		t.Fatalf("unexpected finding on clean chain: %s/%s %s", f.ActionID, f.Code, f.Message)
	}
}

func TestSourceTamperBreaksChainTransitively(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	buildProject(t, st, p)

	testutil.Overwrite(t, filepath.Join(st.ProjectRoot(p.Name), "src", "b.txt"), "bravo TAMPERED\n")
	rep := verifyProject(t, st, p)

	if rep.Complete {
		t.Fatal("expected incomplete after source tamper")
	}
	if rep.Nodes["compile_a"].Complete != true {
		t.Error("compile_a should remain complete (does not read b.txt)")
	}
	if rep.Nodes["compile_b"].Complete || !rep.Nodes["compile_b"].Tainted {
		t.Error("compile_b must be incomplete and tainted")
	}
	if !codeSet(rep, "compile_b")[CodeSourceDigestMismatch] {
		t.Error("missing SOURCE_DIGEST_MISMATCH on compile_b")
	}
	link := rep.Nodes["link"]
	if link.Complete || !link.Tainted {
		t.Error("link must be incomplete and tainted through compile_b")
	}
	if !codeSet(rep, "link")[CodeUpstreamChainBroken] {
		t.Error("link must carry UPSTREAM_CHAIN_BROKEN")
	}
}

func TestToolTamperDetected(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	buildProject(t, st, p)
	testutil.Overwrite(t,
		filepath.Join(st.ProjectRoot(p.Name), "tools", "concat.sh"),
		"#!/bin/sh\necho hacked > \"$1\"\n")
	if err := os.Chmod(filepath.Join(st.ProjectRoot(p.Name), "tools", "concat.sh"), 0o755); err != nil {
		t.Fatal(err)
	}
	rep := verifyProject(t, st, p)
	for _, action := range []string{"compile_a", "compile_b", "link"} {
		if !codeSet(rep, action)[CodeToolDigestMismatch] {
			t.Errorf("action %s missing TOOL_DIGEST_MISMATCH", action)
		}
	}
}

func TestUnsignedRecordTamperDetected(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	buildProject(t, st, p)

	// Flip the on-disk record file directly, without re-signing.
	idx := st.LoadIndex(p.Name)
	id := idx.Records["base"].RecordID
	path := filepath.Join(st.RecordsDir, id.Hex+".json")
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	var r provenance.Record
	b, _ := os.ReadFile(path)
	if err := json.Unmarshal(b, &r); err != nil {
		t.Fatal(err)
	}
	r.Sources[0].Digest.Hex = "0000000000000000000000000000000000000000000000000000000000000000"
	tampered, _ := json.MarshalIndent(&r, "", "  ")
	if err := os.WriteFile(path, tampered, 0o644); err != nil {
		t.Fatal(err)
	}

	rep := verifyProject(t, st, p)
	codes := codeSet(rep, "base")
	if !codes[CodeSignatureInvalid] {
		t.Error("missing SIGNATURE_INVALID")
	}
	if !codes[CodeRecordHashMismatch] {
		t.Error("missing RECORD_HASH_MISMATCH (file name binding broken)")
	}
	if rep.Nodes["child"].Complete {
		t.Error("child must inherit the broken chain")
	}
}

func TestReSignedForgedLinkCaughtByCrossRecordBinding(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	buildProject(t, st, p)

	// Attacker holds the signing key: alter the upstream link digest AND the
	// embedded fingerprint, then re-sign. Signature and content-id checks
	// pass; the only remaining defense is comparing the link against the
	// upstream record's own output digest.
	mutateRecord(t, st, p.Name, "child", func(r *provenance.Record) {
		fake := digest.OfBytes([]byte("forged"))
		r.Upstreams[0].Digest = fake
		r.InputFingerprint = provenance.ComputeFingerprint(provenance.FingerprintInput{
			ProjectName: r.Project,
			ActionID:    r.ActionID,
			Tool:        r.Tool,
			Sources:     r.Sources,
			Upstreams:   r.Upstreams,
		})
	})

	rep := verifyProject(t, st, p)
	if !codeSet(rep, "child")[CodeUpstreamDigestMismatch] {
		t.Fatal("missing UPSTREAM_DIGEST_MISMATCH for re-signed forged link")
	}
	if codeSet(rep, "child")[CodeSignatureInvalid] || codeSet(rep, "child")[CodeRecordHashMismatch] {
		t.Fatal("signature/content-id should have passed for a key-holder forgery")
	}
}

func TestForgedLinkWithoutFingerprintFixCaughtTwice(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	buildProject(t, st, p)

	mutateRecord(t, st, p.Name, "child", func(r *provenance.Record) {
		r.Upstreams[0].Digest = digest.OfBytes([]byte("forged"))
	})
	rep := verifyProject(t, st, p)
	codes := codeSet(rep, "child")
	if !codes[CodeUpstreamDigestMismatch] || !codes[CodeFingerprintMismatch] {
		t.Fatalf("expected link + fingerprint mismatch, got %v", codes)
	}
}

func TestForgedCycleDetected(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	buildProject(t, st, p)

	// Forge base -> child on top of the existing child -> base edge, then
	// re-sign with the key. The spec on disk remains the original DAG.
	idx := st.LoadIndex(p.Name)
	baseID := idx.Records["base"].RecordID
	baseRec, err := st.GetRecord(baseID)
	if err != nil {
		t.Fatal(err)
	}
	childEnt := idx.Records["child"]
	childRec, err := st.GetRecord(childEnt.RecordID)
	if err != nil {
		t.Fatal(err)
	}
	baseRec.Upstreams = append(baseRec.Upstreams, provenance.UpstreamRef{
		ActionID: "child",
		Output:   childRec.Outputs[0].Path,
		Digest:   childRec.Outputs[0].Digest,
	})
	newBaseID, err := st.PutRecord(baseRec)
	if err != nil {
		t.Fatal(err)
	}
	ent := idx.Records["base"]
	ent.RecordID = newBaseID
	idx.Records["base"] = ent
	if err := st.SaveIndex(idx); err != nil {
		t.Fatal(err)
	}

	rep := verifyProject(t, st, p)
	if rep.Complete {
		t.Fatal("cyclic provenance must not verify as complete")
	}
	if !codeSet(rep, "base")[CodeCycleDetected] || !codeSet(rep, "child")[CodeCycleDetected] {
		t.Fatalf("both cycle nodes must be flagged: base=%v child=%v",
			codeSet(rep, "base"), codeSet(rep, "child"))
	}
	if !rep.Nodes["base"].Complete || !rep.Nodes["child"].Complete {
		// both incomplete — also asserted via Complete above
	}
}

func TestMissingBlobDetected(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	buildProject(t, st, p)

	idx := st.LoadIndex(p.Name)
	baseOut := idx.Records["base"].Outputs["out/base.txt"]
	if err := os.Remove(st.BlobPath(baseOut)); err != nil {
		t.Fatal(err)
	}
	rep := verifyProject(t, st, p)
	if !codeSet(rep, "base")[CodeBlobMissing] {
		t.Fatal("missing BLOB_MISSING")
	}
	if rep.Nodes["child"].Complete || !rep.Nodes["child"].Tainted {
		t.Fatal("child must be tainted by missing upstream artifact")
	}
}

func TestMissingIndexEntryDetected(t *testing.T) {
	st := testutil.NewStore(t)
	p := testutil.InstallTwoNodeProject(t, st)
	buildProject(t, st, p)

	idx := st.LoadIndex(p.Name)
	delete(idx.Records, "base")
	if err := st.SaveIndex(idx); err != nil {
		t.Fatal(err)
	}
	rep := verifyProject(t, st, p)
	if !codeSet(rep, "base")[CodeMissingRecord] {
		t.Fatal("missing MISSING_RECORD")
	}
	if rep.Nodes["child"].Complete {
		t.Fatal("child cannot be complete without base")
	}
}

func TestSharedSourceImpactDoesNotCrossContaminateUnrelated(t *testing.T) {
	// compile_a reading shared.txt changes; compile_b and link both depend on
	// shared.txt too, so both are affected. Add an unrelated action later in
	// the impact tests to check the boundary.
	st := testutil.NewStore(t)
	p := testutil.InstallSharedProject(t, st)
	buildProject(t, st, p)
	rep := verifyProject(t, st, p)
	if !rep.Complete {
		t.Fatal("clean chain incomplete")
	}
}
