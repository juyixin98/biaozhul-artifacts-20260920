// Package manifestx parses OCI / Docker Distribution v2 manifest documents and
// extracts their reference graph (the edges the GC marker walks).
package manifestx

import (
	"encoding/json"
	"errors"
	"fmt"

	"layerregistry/internal/digestx"
)

// Media types we understand. Unknown-but-JSON manifests are rejected: the GC
// must know the full reference graph to be safe.
const (
	OCIManifestMediaType = "application/vnd.oci.image.manifest.v1+json"
	DockerManifestV2     = "application/vnd.docker.distribution.manifest.v2+json"
	OCIIndexMediaType    = "application/vnd.oci.image.index.v1+json"
	DockerManifestList   = "application/vnd.docker.distribution.manifest.list.v2+json"
)

var manifestMediaTypes = map[string]bool{
	OCIManifestMediaType: true,
	DockerManifestV2:     true,
	OCIIndexMediaType:    true,
	DockerManifestList:   true,
}

// IsManifestMediaType reports whether mt names a supported manifest type.
func IsManifestMediaType(mt string) bool { return manifestMediaTypes[mt] }

func isImageManifest(mt string) bool { return mt == OCIManifestMediaType || mt == DockerManifestV2 }
func isIndex(mt string) bool         { return mt == OCIIndexMediaType || mt == DockerManifestList }

// ChildRef is one edge in the manifest DAG.
type ChildRef struct {
	Kind   string // "blob" or "manifest"
	Digest string
}

type descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
}

type imageManifest struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	Config        *descriptor  `json:"config"`
	Layers        []descriptor `json:"layers"`
	Subject       *descriptor  `json:"subject"`
}

type indexManifest struct {
	SchemaVersion int          `json:"schemaVersion"`
	MediaType     string       `json:"mediaType"`
	Manifests     []descriptor `json:"manifests"`
	Subject       *descriptor  `json:"subject"`
}

var ErrUnsupportedManifest = errors.New("unsupported or malformed manifest")

// Parse validates the wire bytes, classifies the document and returns its
// outgoing references. mediaType is taken from the Content-Type/declared
// type; Docker schema2 historically allows the body to omit mediaType.
func Parse(content []byte, mediaType string) ([]ChildRef, error) {
	if !manifestMediaTypes[mediaType] {
		return nil, fmt.Errorf("%w: media type %q", ErrUnsupportedManifest, mediaType)
	}

	refs := make([]ChildRef, 0, 4)
	add := func(kind string, d descriptor) error {
		if !digestx.Valid(d.Digest) {
			return fmt.Errorf("%w: descriptor has invalid digest %q", ErrUnsupportedManifest, d.Digest)
		}
		refs = append(refs, ChildRef{Kind: kind, Digest: d.Digest})
		return nil
	}

	if isImageManifest(mediaType) {
		var m imageManifest
		if err := json.Unmarshal(content, &m); err != nil {
			return nil, fmt.Errorf("%w: %v", ErrUnsupportedManifest, err)
		}
		if m.SchemaVersion != 2 {
			return nil, fmt.Errorf("%w: schemaVersion must be 2, got %d", ErrUnsupportedManifest, m.SchemaVersion)
		}
		if m.Config == nil || m.Config.Digest == "" {
			return nil, fmt.Errorf("%w: image manifest requires a config descriptor", ErrUnsupportedManifest)
		}
		if err := add("blob", *m.Config); err != nil {
			return nil, err
		}
		for _, l := range m.Layers {
			if err := add("blob", l); err != nil {
				return nil, err
			}
		}
		if m.Subject != nil {
			// Referrers API: subject is another manifest.
			if err := add("manifest", *m.Subject); err != nil {
				return nil, err
			}
		}
		return refs, nil
	}

	// Image index / manifest list.
	var ix indexManifest
	if err := json.Unmarshal(content, &ix); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnsupportedManifest, err)
	}
	if ix.SchemaVersion != 2 {
		return nil, fmt.Errorf("%w: schemaVersion must be 2, got %d", ErrUnsupportedManifest, ix.SchemaVersion)
	}
	for _, e := range ix.Manifests {
		if err := add("manifest", e); err != nil {
			return nil, err
		}
	}
	if ix.Subject != nil {
		if err := add("manifest", *ix.Subject); err != nil {
			return nil, err
		}
	}
	return refs, nil
}
