package oci_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ociarch/internal/fixture"
	"ociarch/internal/oci"
)

func TestImportTwoArch(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	root, err := fixture.Build(dir, "two-arch")
	if err != nil {
		t.Fatal(err)
	}
	g, err := oci.ImportLayout(dir)
	if err != nil {
		t.Fatalf("import: %v", err)
	}
	if g.Root != root {
		t.Fatalf("root digest: got %s want %s", g.Root, root)
	}
	// 1 index + 2 manifests + 2 configs + 4 layers = 9 verified nodes.
	if len(g.Nodes) != 9 {
		t.Fatalf("want 9 nodes, got %d", len(g.Nodes))
	}
	// 2 manifest edges + 2 config edges + 4 layer edges.
	counts := map[string]int{}
	for _, e := range g.Edges {
		counts[e.Kind]++
	}
	if counts[oci.EdgeManifest] != 2 || counts[oci.EdgeConfig] != 2 || counts[oci.EdgeLayer] != 4 {
		t.Fatalf("unexpected edge counts: %v", counts)
	}

	// Full chain for the arm64 manifest must be reconstructable.
	var armDigest string
	for _, e := range g.Edges {
		if e.Kind == oci.EdgeManifest && e.Platform.Architecture == "arm64" {
			armDigest = e.Child
		}
	}
	chain, err := g.Chain(armDigest)
	if err != nil {
		t.Fatal(err)
	}
	// root -> manifest -> config -> 2 layers
	if len(chain) != 5 {
		t.Fatalf("want 5 chain entries, got %d: %+v", len(chain), chain)
	}
	if chain[0].Child != g.Root || chain[1].Child != armDigest {
		t.Fatalf("chain ordering wrong: %+v", chain)
	}
}

func TestImportBadConfigPlatform(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	if _, err := fixture.Build(dir, "bad-config"); err != nil {
		t.Fatal(err)
	}
	_, err := oci.ImportLayout(dir)
	if err == nil || !strings.Contains(err.Error(), "does not match descriptor platform") {
		t.Fatalf("want config/descriptor platform mismatch error, got %v", err)
	}
}

func TestImportMissingLayer(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	if _, err := fixture.Build(dir, "missing-layer"); err != nil {
		t.Fatal(err)
	}
	_, err := oci.ImportLayout(dir)
	if err == nil || !strings.Contains(err.Error(), "layer 1") {
		t.Fatalf("want layer 1 verification error, got %v", err)
	}
}

func TestImportDigestMismatchOnTamper(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	if _, err := fixture.Build(dir, "two-arch"); err != nil {
		t.Fatal(err)
	}
	// Tamper with an arbitrary layer blob: its digest must stop matching.
	blobs := filepath.Join(dir, "blobs", "sha256")
	entries, err := os.ReadDir(blobs)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(blobs, entries[0].Name())
	if err := os.WriteFile(target, []byte("tampered content that changes the hash\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = oci.ImportLayout(dir)
	if err == nil || !strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("want digest mismatch, got %v", err)
	}
}

func TestImportSizeMismatchInDescriptor(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	if _, err := fixture.Build(dir, "two-arch"); err != nil {
		t.Fatal(err)
	}
	// Rewrite index.json: keep the manifest digest but lie about its size.
	// Root digest is recomputed from index.json, so this specifically drives
	// the size-check path while the digest still matches the blob.
	idxRaw, err := os.ReadFile(filepath.Join(dir, "index.json"))
	if err != nil {
		t.Fatal(err)
	}
	var idx oci.Index
	if err := json.Unmarshal(idxRaw, &idx); err != nil {
		t.Fatal(err)
	}
	idx.Manifests[0].Size++
	out, _ := json.MarshalIndent(idx, "", "  ")
	if err := os.WriteFile(filepath.Join(dir, "index.json"), out, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = oci.ImportLayout(dir)
	if err == nil || !strings.Contains(err.Error(), "size mismatch") {
		t.Fatalf("want size mismatch, got %v", err)
	}
}

func TestImportNestedIndexDiamond(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	b := fixture.New(dir)
	m1, err := b.AddImageDetached(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
		[][]byte{[]byte("shared-diamond amd64\n")}, -1)
	if err != nil {
		t.Fatal(err)
	}
	m2, err := b.AddImageDetached(oci.Platform{OS: "linux", Architecture: "arm64"}, nil,
		[][]byte{[]byte("shared-diamond arm64\n")}, -1)
	if err != nil {
		t.Fatal(err)
	}
	idxA, err := b.AddNestedIndex([]oci.Descriptor{m1, m2})
	if err != nil {
		t.Fatal(err)
	}
	idxB, err := b.AddNestedIndex([]oci.Descriptor{m2}) // shares m2 with idxA
	if err != nil {
		t.Fatal(err)
	}
	b.AddDescriptor(idxA)
	b.AddDescriptor(idxB)
	if _, err := b.WriteIndex(); err != nil {
		t.Fatal(err)
	}

	g, err := oci.ImportLayout(dir)
	if err != nil {
		t.Fatalf("import: %v", err)
	}
	if _, ok := g.Nodes[m2.Digest]; !ok {
		t.Fatal("shared manifest missing from graph")
	}
	// The shared node appears as an edge from both nested indexes, but the
	// node itself was stored exactly once (no infinite loop, no duplication).
	n := 0
	for _, e := range g.Edges {
		if e.Child == m2.Digest && e.Kind == oci.EdgeManifest {
			n++
		}
	}
	if n != 2 {
		t.Fatalf("want 2 manifest edges to shared node, got %d", n)
	}
}

// TestImportMalformedDigest rejects index entries that do not parse before
// any blob is read.
func TestImportMalformedDigest(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "layout")
	b := fixture.New(dir)
	m1, err := b.AddImageDetached(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
		[][]byte{[]byte("x\n")}, -1)
	if err != nil {
		t.Fatal(err)
	}
	m1.Digest = "not-a-digest"
	idx, err := b.AddNestedIndex([]oci.Descriptor{m1})
	if err != nil {
		t.Fatal(err)
	}
	b.AddDescriptor(idx)
	if _, err := b.WriteIndex(); err != nil {
		t.Fatal(err)
	}
	if _, err := oci.ImportLayout(dir); err == nil {
		t.Fatal("want malformed digest error, got nil")
	}
}
