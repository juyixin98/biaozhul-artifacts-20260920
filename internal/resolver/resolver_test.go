package resolver_test

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/fixture"
	"github.example.com/ocimultipick/internal/oci"
	"github.example.com/ocimultipick/internal/resolver"
	"github.example.com/ocimultipick/internal/selector"
)

// buildImage creates one config+single-layer image in b and returns its
// descriptor with the given platform attached.
func buildImage(t *testing.T, b *fixture.Builder, name, goos, arch, variant, payload string) oci.Descriptor {
	t.Helper()
	content := fixture.LayerContent(name, []byte(payload))
	layer, err := b.AddLayer(name, content)
	if err != nil {
		t.Fatalf("add layer: %v", err)
	}
	cfg, err := b.AddConfig(goos, arch, variant, []string{fixture.DiffIDUncompressed(content)})
	if err != nil {
		t.Fatalf("add config: %v", err)
	}
	manifest, err := b.AddManifest(cfg, []oci.Descriptor{layer})
	if err != nil {
		t.Fatalf("add manifest: %v", err)
	}
	manifest.Platform = &oci.Platform{OS: goos, Architecture: arch, Variant: variant}
	return manifest
}

func blobPath(t *testing.T, st *blobstore.Store, repo, dgst string) string {
	t.Helper()
	d, err := digest.Parse(dgst)
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(st.Root(), repo, "blobs", d.Algorithm(), d.Encoded())
}

func TestTwoArchitectureHappyPath(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/app")
	amd := buildImage(t, b, "amd", "linux", "amd64", "", "x86 rootfs")
	arm := buildImage(t, b, "arm", "linux", "arm64", "", "arm rootfs")
	root, _, err := b.AddIndex([]oci.Descriptor{amd, arm})
	if err != nil {
		t.Fatal(err)
	}
	rootD, _ := digest.Parse(root.Digest)
	rsv := resolver.New(st)

	for _, tc := range []struct {
		platform   selector.Platform
		wantDigest string
	}{
		{selector.Platform{OS: "linux", Arch: "amd64"}, amd.Digest},
		{selector.Platform{OS: "linux", Arch: "arm64"}, arm.Digest},
	} {
		res, err := rsv.Resolve("demo/app", rootD, tc.platform)
		if err != nil {
			t.Fatalf("resolve %s: %v", tc.platform, err)
		}
		if res.Manifest.Digest != tc.wantDigest {
			t.Fatalf("resolve %s picked %s, want %s", tc.platform, res.Manifest.Digest, tc.wantDigest)
		}
		// Full dependency chain: root index, manifest, config, exactly one layer.
		roles := make([]string, len(res.Chain))
		for i, c := range res.Chain {
			roles[i] = c.Role
			if c.Digest == "" || c.Size < 0 {
				t.Fatalf("chain entry %d missing digest/size: %+v", i, c)
			}
		}
		if len(roles) != 4 {
			t.Fatalf("chain length = %d (%v), want 4 (index,manifest,config,layer)", len(roles), roles)
		}
		if roles[0] != "index" || roles[1] != "manifest" || roles[2] != "config" || roles[3] != "layer" {
			t.Fatalf("unexpected chain roles: %v", roles)
		}
		if res.Chain[0].Digest != root.Digest {
			t.Fatalf("chain root = %s, want index %s", res.Chain[0].Digest, root.Digest)
		}
	}
}

func TestWrongPlatformRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/app")
	amd := buildImage(t, b, "amd", "linux", "amd64", "", "x86")
	arm := buildImage(t, b, "arm", "linux", "arm64", "", "arm")
	root, _, _ := b.AddIndex([]oci.Descriptor{amd, arm})
	rootD, _ := digest.Parse(root.Digest)

	_, err := resolver.New(st).Resolve("demo/app", rootD, selector.Platform{OS: "darwin", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeNoMatch {
		t.Fatalf("want no_platform_match, got %v", err)
	}
}

func TestBadConfigPlatformRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/bad")
	// Real config blob says linux/386, but the index advertises linux/arm64.
	m := buildImage(t, b, "m", "linux", "386", "", "i386 bytes")
	m.Platform = &oci.Platform{OS: "linux", Architecture: "arm64"}
	root, _, _ := b.AddIndex([]oci.Descriptor{m})
	rootD, _ := digest.Parse(root.Digest)

	_, err := resolver.New(st).Resolve("demo/bad", rootD, selector.Platform{OS: "linux", Arch: "arm64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeConfigPlatform {
		t.Fatalf("want config_platform_mismatch, got %v", err)
	}
}

func TestMissingLayerRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/missing")
	content := fixture.LayerContent("m", []byte("ghost layer"))
	layer, err := b.AddLayer("m", content)
	if err != nil {
		t.Fatal(err)
	}
	cfg, _ := b.AddConfig("linux", "amd64", "", []string{fixture.DiffIDUncompressed(content)})
	manifest, _ := b.AddManifest(cfg, []oci.Descriptor{layer})
	// Delete only the layer blob; the manifest still references it.
	if err := os.Remove(blobPath(t, st, "demo/missing", layer.Digest)); err != nil {
		t.Fatal(err)
	}
	manifest.Platform = &oci.Platform{OS: "linux", Architecture: "amd64"}
	root, _, _ := b.AddIndex([]oci.Descriptor{manifest})
	rootD, _ := digest.Parse(root.Digest)

	_, err = resolver.New(st).Resolve("demo/missing", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeBlobMissing {
		t.Fatalf("want blob_missing for absent layer, got %v", err)
	}
}

func TestAmbiguousCandidatesRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/amb")
	a := buildImage(t, b, "a", "linux", "amd64", "", "A")
	c := buildImage(t, b, "c", "linux", "amd64", "", "C")
	root, _, _ := b.AddIndex([]oci.Descriptor{a, c})
	rootD, _ := digest.Parse(root.Digest)

	_, err := resolver.New(st).Resolve("demo/amb", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeAmbiguous {
		t.Fatalf("want platform_ambiguous, got %v", err)
	}
}

func TestLyingDigestInIndexRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/lie")
	m := buildImage(t, b, "m", "linux", "amd64", "", "ok")
	// Build an index whose child descriptor claims a digest that has no bytes
	// on disk: traversal must report the missing blob, never read the wrong one.
	_, idx, _ := b.AddIndex([]oci.Descriptor{m})
	idx.Manifests[0].Digest = "sha256:000000000000000000000000000000000000000000000000000000000000dead"
	raw, _ := json.MarshalIndent(idx, "", "  ")
	rootDesc, _ := st.PutBytes("demo/lie", "sha256", oci.MediaTypeImageIndex, raw)
	rootD, _ := digest.Parse(rootDesc.Digest)

	_, err := resolver.New(st).Resolve("demo/lie", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeBlobMissing {
		t.Fatalf("want blob_missing when descriptor digest has no bytes, got %v", err)
	}
}

func TestSizeMismatchInIndexRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/size")
	m := buildImage(t, b, "m", "linux", "amd64", "", "ok")
	root, idx, _ := b.AddIndex([]oci.Descriptor{m})
	// Rewrite the index on disk after the fact declaring a wrong child size;
	// keep the index's own descriptor consistent by re-hashing.
	idx.Manifests[0].Size = m.Size + 123
	raw, _ := json.MarshalIndent(idx, "", "  ")
	rootDesc, _ := st.PutBytes("demo/size", "sha256", oci.MediaTypeImageIndex, raw)
	rootD, _ := digest.Parse(rootDesc.Digest)
	_ = root

	_, err := resolver.New(st).Resolve("demo/size", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeSizeMismatch {
		t.Fatalf("want size_mismatch, got %v", err)
	}
}

func TestTamperedLayerBytesRejected(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/tamper")
	m := buildImage(t, b, "m", "linux", "amd64", "", "original")
	root, _, _ := b.AddIndex([]oci.Descriptor{m})

	// Pull the layer digest out of the stored manifest and tamper that blob.
	var man oci.Manifest
	mbody, err := os.ReadFile(blobPath(t, st, "demo/tamper", m.Digest))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(mbody, &man); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(blobPath(t, st, "demo/tamper", man.Layers[0].Digest),
		[]byte("attacker controlled layer bytes"), 0o644); err != nil {
		t.Fatal(err)
	}

	rootD, _ := digest.Parse(root.Digest)
	_, err = resolver.New(st).Resolve("demo/tamper", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	var re *resolver.Error
	if !errors.As(err, &re) || re.Code != resolver.CodeDigestMismatch {
		t.Fatalf("want digest_mismatch for tampered layer, got %v", err)
	}
}

func TestNestedIndexChain(t *testing.T) {
	st := blobstore.New(t.TempDir())
	b := fixture.NewBuilder(st, "demo/nested")
	amd := buildImage(t, b, "amd", "linux", "amd64", "", "x86")
	// Intermediate index holds the real manifest; root index points at it.
	mid, _, err := b.AddIndex([]oci.Descriptor{amd})
	if err != nil {
		t.Fatal(err)
	}
	mid.Platform = amd.Platform // nested indexes may carry the platform through
	root, _, err := b.AddIndex([]oci.Descriptor{mid})
	if err != nil {
		t.Fatal(err)
	}
	rootD, _ := digest.Parse(root.Digest)

	res, err := resolver.New(st).Resolve("demo/nested", rootD, selector.Platform{OS: "linux", Arch: "amd64"})
	if err != nil {
		t.Fatal(err)
	}
	if res.Manifest.Digest != amd.Digest {
		t.Fatalf("nested selection = %s, want %s", res.Manifest.Digest, amd.Digest)
	}
	// Chain must contain both indexes before manifest/config/layer.
	roles := make([]string, len(res.Chain))
	for i, c := range res.Chain {
		roles[i] = c.Role
	}
	wantRoles := []string{"index", "index", "manifest", "config", "layer"}
	if len(roles) != len(wantRoles) {
		t.Fatalf("chain roles %v, want %v", roles, wantRoles)
	}
	for i := range wantRoles {
		if roles[i] != wantRoles[i] {
			t.Fatalf("chain roles %v, want %v", roles, wantRoles)
		}
	}
}
