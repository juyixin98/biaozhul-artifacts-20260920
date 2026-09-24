// Package fixture builds deterministic, genuinely hashed OCI content for
// tests and the testdata generator. Layer/config/index bytes are real and
// every digest is a real SHA-256 over those bytes; helpers also exist to
// deliberately corrupt descriptors and delete blobs so failure paths can be
// exercised against real verification, not mocks.
package fixture

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.example.com/ocimultipick/internal/blobstore"
	"github.example.com/ocimultipick/internal/digest"
	"github.example.com/ocimultipick/internal/oci"
)

// Builder accumulates blobs inside one repository of a blob store.
type Builder struct {
	store *blobstore.Store
	repo  string
}

// NewBuilder returns a builder writing into repo of store.
func NewBuilder(store *blobstore.Store, repo string) *Builder {
	return &Builder{store: store, repo: repo}
}

// LayerContent creates a deterministic gzip-like layer payload.
func LayerContent(name string, payload []byte) []byte {
	// Not a real gzip stream: the service never decompresses layers, it only
	// verifies digest/size. The framing below keeps payloads distinct and
	// human-readable in examples.
	return bytes.Join([][]byte{
		[]byte("# fake-layer framing for " + name + "\n"),
		payload,
		[]byte("\n# end layer " + name + "\n"),
	}, nil)
}

// AddLayer stores a layer blob and returns its descriptor (real digest/size).
func (b *Builder) AddLayer(name string, content []byte) (oci.Descriptor, error) {
	return b.store.PutBytes(b.repo, "sha256", oci.MediaTypeLayerGzip, content)
}

// AddConfig stores an image config blob. diffIDs must correspond to layers.
func (b *Builder) AddConfig(goos, arch, variant string, diffIDs []string) (oci.Descriptor, error) {
	cfg := oci.ImageConfig{
		OS:           goos,
		Architecture: arch,
		Variant:      variant,
		RootFS: &oci.RootFS{
			Type:    "layers",
			DiffIDs: diffIDs,
		},
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return oci.Descriptor{}, err
	}
	return b.store.PutBytes(b.repo, "sha256", oci.MediaTypeImageConfig, raw)
}

// AddManifest stores an image manifest assembled from config and layers.
func (b *Builder) AddManifest(config oci.Descriptor, layers []oci.Descriptor) (oci.Descriptor, error) {
	m := oci.Manifest{
		SchemaVersion: 2,
		MediaType:     oci.MediaTypeImageManifest,
		Config:        config,
		Layers:        layers,
	}
	raw, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return oci.Descriptor{}, err
	}
	return b.store.PutBytes(b.repo, "sha256", oci.MediaTypeImageManifest, raw)
}

// AddIndex stores an image index over child descriptors.
func (b *Builder) AddIndex(children []oci.Descriptor) (oci.Descriptor, oci.Index, error) {
	idx := oci.Index{SchemaVersion: 2, MediaType: oci.MediaTypeImageIndex, Manifests: children}
	raw, err := json.MarshalIndent(idx, "", "  ")
	if err != nil {
		return oci.Descriptor{}, idx, err
	}
	desc, err := b.store.PutBytes(b.repo, "sha256", oci.MediaTypeImageIndex, raw)
	return desc, idx, err
}

// Tag records a tag pointer in the repository's index.json (the OCI-layout
// convention). The application seeds its SQLite tag table from this on boot.
func (b *Builder) Tag(tag string, target oci.Descriptor) error {
	idx, err := b.store.LoadIndex(b.repo)
	if err != nil {
		return err
	}
	if target.Annotations == nil {
		target.Annotations = map[string]string{}
	}
	target.Annotations[oci.AnnotationRefName] = tag

	kept := idx.Manifests[:0]
	for _, d := range idx.Manifests {
		if d.Annotations[oci.AnnotationRefName] != tag {
			kept = append(kept, d)
		}
	}
	kept = append(kept, target)
	idx.Manifests = kept
	return b.store.SaveIndex(b.repo, idx)
}

// CorruptDigest rewrites a descriptor's Digest field to another valid-looking
// digest while leaving the blob bytes untouched, simulating an index that
// lies about content.
func CorruptDigest(d oci.Descriptor) oci.Descriptor {
	fake := "sha256:000000000000000000000000000000000000000000000000000000000000dead"
	d.Digest = fake
	return d
}

// CorruptSize changes the declared size of a descriptor.
func CorruptSize(d oci.Descriptor) oci.Descriptor {
	if d.Size == 42 {
		d.Size = 43
	} else {
		d.Size = 42
	}
	return d
}

// DeleteBlob removes the blob a descriptor points at (missing layer / config).
func (b *Builder) DeleteBlob(d oci.Descriptor) error {
	dg, err := digest.Parse(d.Digest)
	if err != nil {
		return err
	}
	return b.store.Remove(b.repo, dg)
}

// DiffIDUncompressed returns a plausible diffID for raw layer content.
func DiffIDUncompressed(content []byte) string {
	d, _ := digest.FromBytes("sha256", content)
	return d.String()
}

// Must panics on error — helper for generators and tests where a failure is
// a programming error.
func Must[T any](v T, err error) T {
	if err != nil {
		panic(fmt.Sprintf("fixture build error: %v", err))
	}
	return v
}
