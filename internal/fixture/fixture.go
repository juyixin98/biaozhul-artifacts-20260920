// Package fixture builds small, deterministic OCI image layouts on disk for
// tests and for the fixturegen command. No network is ever touched.
package fixture

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"ociarch/internal/oci"
)

// Builder accumulates manifests and finally writes index.json.
type Builder struct {
	dir   string
	descs []oci.Descriptor
}

func New(dir string) *Builder { return &Builder{dir: dir} }

func (b *Builder) writeBlob(content []byte) (digest string, size int64, err error) {
	digest = oci.DigestBytes(content)
	p := filepath.Join(b.dir, "blobs", "sha256", digest[len("sha256:"):])
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return "", 0, err
	}
	if err := os.WriteFile(p, content, 0o644); err != nil {
		return "", 0, err
	}
	return digest, int64(len(content)), nil
}

// AddImageDetached writes a config blob and layer blobs and returns the
// manifest descriptor WITHOUT registering it in the root index (used to
// compose nested indexes). See AddImage for parameter semantics.
func (b *Builder) AddImageDetached(p oci.Platform, cfgOverride *oci.Platform, layers [][]byte, skipLayerBlob int) (oci.Descriptor, error) {
	cfgPlatform := p
	if cfgOverride != nil {
		cfgPlatform = *cfgOverride
	}
	cfgDoc, err := json.Marshal(map[string]any{
		"architecture": cfgPlatform.Architecture,
		"os":           cfgPlatform.OS,
		"variant":      cfgPlatform.Variant,
		"created":      "2026-01-01T00:00:00Z",
		"rootfs":       map[string]any{"type": "layers", "diff_ids": []string{}},
	})
	if err != nil {
		return oci.Descriptor{}, err
	}
	cfgDigest, cfgSize, err := b.writeBlob(cfgDoc)
	if err != nil {
		return oci.Descriptor{}, err
	}

	m := oci.Manifest{
		MediaType: oci.MediaTypeManifestOCI,
		Config: oci.Descriptor{
			MediaType: oci.MediaTypeConfigOCI,
			Digest:    cfgDigest,
			Size:      cfgSize,
		},
	}
	for i, content := range layers {
		digest := oci.DigestBytes(content)
		if i != skipLayerBlob {
			if _, _, err := b.writeBlob(content); err != nil {
				return oci.Descriptor{}, err
			}
		}
		m.Layers = append(m.Layers, oci.Descriptor{
			MediaType: "application/vnd.oci.image.layer.v1.tar",
			Digest:    digest,
			Size:      int64(len(content)),
		})
	}

	manifestDoc, err := json.Marshal(m)
	if err != nil {
		return oci.Descriptor{}, err
	}
	manifestDigest, manifestSize, err := b.writeBlob(manifestDoc)
	if err != nil {
		return oci.Descriptor{}, err
	}
	plat := p
	return oci.Descriptor{
		MediaType: oci.MediaTypeManifestOCI,
		Digest:    manifestDigest,
		Size:      manifestSize,
		Platform:  &plat,
	}, nil
}

// AddImage is AddImageDetached plus registration in the root index.
// cfgOverride, when non-nil, replaces the platform written into the config
// document (used to build the wrong-config-platform fixture).
// skipLayerBlob >= 0 omits writing that layer's blob (missing-layer fixture)
// while still referencing it from the manifest.
func (b *Builder) AddImage(p oci.Platform, cfgOverride *oci.Platform, layers [][]byte, skipLayerBlob int) (oci.Descriptor, error) {
	desc, err := b.AddImageDetached(p, cfgOverride, layers, skipLayerBlob)
	if err != nil {
		return oci.Descriptor{}, err
	}
	b.descs = append(b.descs, desc)
	return desc, nil
}

// AddNestedIndex writes an index blob containing child descriptors (which may
// be manifests or further indexes) and registers it as a child of the root.
func (b *Builder) AddNestedIndex(children []oci.Descriptor) (oci.Descriptor, error) {
	doc, err := json.MarshalIndent(oci.Index{MediaType: oci.MediaTypeIndexOCI, Manifests: children}, "", "  ")
	if err != nil {
		return oci.Descriptor{}, err
	}
	digest, size, err := b.writeBlob(doc)
	if err != nil {
		return oci.Descriptor{}, err
	}
	return oci.Descriptor{MediaType: oci.MediaTypeIndexOCI, Digest: digest, Size: size}, nil
}

// AddDescriptor registers a prebuilt descriptor (e.g. a nested index) in the
// root index.
func (b *Builder) AddDescriptor(d oci.Descriptor) {
	b.descs = append(b.descs, d)
}

// WriteIndex writes index.json and oci-layout, returning the root digest.
func (b *Builder) WriteIndex() (string, error) {
	idx := oci.Index{MediaType: oci.MediaTypeIndexOCI, Manifests: b.descs}
	doc, err := json.MarshalIndent(idx, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(b.dir, 0o755); err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(b.dir, "index.json"), doc, 0o644); err != nil {
		return "", err
	}
	layout := []byte(`{"imageLayoutVersion":"1.0.0"}`)
	if err := os.WriteFile(filepath.Join(b.dir, "oci-layout"), layout, 0o644); err != nil {
		return "", err
	}
	return oci.DigestBytes(doc), nil
}

// Build writes one of the named fixtures into dir and returns its root digest.
//
//	two-arch:      linux/amd64 + linux/arm64, healthy
//	bad-config:    descriptor says linux/arm64 but config says linux/amd64
//	missing-layer: amd64 manifest references a layer blob that is absent
//	ambiguous:     two distinct linux/amd64 manifests, no variants
//	arm-variants:  linux/arm/v7 + linux/arm/v8 (variant rules)
func Build(dir, name string) (string, error) {
	b := New(dir)
	switch name {
	case "two-arch":
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
			[][]byte{[]byte("amd64 layer 1\n"), []byte("amd64 layer 2\n")}, -1); err != nil {
			return "", err
		}
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "arm64"}, nil,
			[][]byte{[]byte("arm64 layer 1\n"), []byte("arm64 layer 2\n")}, -1); err != nil {
			return "", err
		}
	case "bad-config":
		if _, err := b.AddImage(
			oci.Platform{OS: "linux", Architecture: "arm64"},
			&oci.Platform{OS: "linux", Architecture: "amd64"}, // config lies
			[][]byte{[]byte("bad-config layer\n")}, -1); err != nil {
			return "", err
		}
	case "missing-layer":
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
			[][]byte{[]byte("present layer\n"), []byte("absent layer\n")}, 1); err != nil {
			return "", err
		}
	case "ambiguous":
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
			[][]byte{[]byte("amd64 build A\n")}, -1); err != nil {
			return "", err
		}
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "amd64"}, nil,
			[][]byte{[]byte("amd64 build B\n")}, -1); err != nil {
			return "", err
		}
	case "arm-variants":
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "arm", Variant: "v7"}, nil,
			[][]byte{[]byte("armv7 layer\n")}, -1); err != nil {
			return "", err
		}
		if _, err := b.AddImage(oci.Platform{OS: "linux", Architecture: "arm", Variant: "v8"}, nil,
			[][]byte{[]byte("armv8 layer\n")}, -1); err != nil {
			return "", err
		}
	default:
		return "", fmt.Errorf("unknown fixture %q", name)
	}
	return b.WriteIndex()
}
