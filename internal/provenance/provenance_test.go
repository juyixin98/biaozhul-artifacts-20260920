package provenance

import (
	"strings"
	"testing"

	"bis/internal/digest"
)

func sampleInputs(swap bool) FingerprintInput {
	srcs := []SourceRef{
		{Path: "src/a.txt", Digest: digest.OfBytes([]byte("A"))},
		{Path: "src/b.txt", Digest: digest.OfBytes([]byte("B"))},
	}
	ups := []UpstreamRef{
		{ActionID: "x", Output: "out/x.bin", Digest: digest.OfBytes([]byte("X"))},
		{ActionID: "y", Output: "out/y.bin", Digest: digest.OfBytes([]byte("Y"))},
	}
	if swap {
		srcs[0], srcs[1] = srcs[1], srcs[0]
		ups[0], ups[1] = ups[1], ups[0]
	}
	return FingerprintInput{
		ProjectName: "p",
		Tool:        ToolRef{Path: "tools/concat.sh", Args: []string{"o", "i"}, Digest: digest.OfBytes([]byte("T"))},
		Sources:     srcs,
		Upstreams:   ups,
	}
}

func TestFingerprintOrderIndependent(t *testing.T) {
	a := ComputeFingerprint(sampleInputs(false))
	b := ComputeFingerprint(sampleInputs(true))
	if !a.Equal(b) {
		t.Fatalf("fingerprint changed with input ordering:\n%s\n%s", a, b)
	}
}

func TestFingerprintChangesOnAnyInput(t *testing.T) {
	base := ComputeFingerprint(sampleInputs(false))

	changed := sampleInputs(false)
	changed.Sources[0].Digest = digest.OfBytes([]byte("A2"))
	if got := ComputeFingerprint(changed); got.Equal(base) {
		t.Fatal("fingerprint unchanged after source edit")
	}

	changed = sampleInputs(false)
	changed.Tool.Digest = digest.OfBytes([]byte("T2"))
	if got := ComputeFingerprint(changed); got.Equal(base) {
		t.Fatal("fingerprint unchanged after tool edit")
	}

	changed = sampleInputs(false)
	changed.Tool.Args = []string{"o", "i2"}
	if got := ComputeFingerprint(changed); got.Equal(base) {
		t.Fatal("fingerprint unchanged after args edit")
	}

	changed = sampleInputs(false)
	changed.Upstreams[0].Digest = digest.OfBytes([]byte("X2"))
	if got := ComputeFingerprint(changed); got.Equal(base) {
		t.Fatal("fingerprint unchanged after upstream edit")
	}
}

func TestSignAndVerify(t *testing.T) {
	key := []byte("secret-key")
	r := &Record{Schema: CanonicalVersion, Project: "p", ActionID: "a"}
	if err := r.Sign(key); err != nil {
		t.Fatal(err)
	}
	if err := r.VerifySig(key); err != nil {
		t.Fatalf("valid signature rejected: %v", err)
	}
	if err := r.VerifySig([]byte("other-key")); err == nil {
		t.Fatal("signature verified with wrong key")
	}
	sig := r.Sig
	r.ActionID = "b"
	if err := r.VerifySig(key); err == nil {
		t.Fatal("tampered body verified")
	}
	r.ActionID = "a"
	r.Sig = strings.TrimSuffix(sig, "0") + "1"
	if err := r.VerifySig(key); err == nil {
		t.Fatal("forged signature verified")
	}
}

func TestContentIDBindsBody(t *testing.T) {
	key := []byte("k")
	r1 := &Record{Project: "p", ActionID: "a"}
	_ = r1.Sign(key)
	id1, _ := ContentID(r1)
	r2 := &Record{Project: "p", ActionID: "b"}
	_ = r2.Sign(key)
	id2, _ := ContentID(r2)
	if id1.Equal(id2) {
		t.Fatal("different records share a content id")
	}
}
