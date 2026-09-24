// Package oci defines the subset of the OCI Image Specification types used by
// this service: image indexes, image manifests, and image configs.
package oci

// Media types defined by the OCI Image Specification.
const (
	MediaTypeImageIndex    = "application/vnd.oci.image.index.v1+json"
	MediaTypeImageManifest = "application/vnd.oci.image.manifest.v1+json"
	MediaTypeImageConfig   = "application/vnd.oci.image.config.v1+json"

	// Layer media types.
	MediaTypeLayer     = "application/vnd.oci.image.layer.v1.tar"
	MediaTypeLayerGzip = "application/vnd.oci.image.layer.v1.tar+gzip"
	MediaTypeLayerZstd = "application/vnd.oci.image.layer.v1.tar+zstd"
	MediaTypeEmptyJSON = "application/vnd.oci.empty.v1+json"
)

// Descriptor describes a content-addressed blob, as defined in
// https://github.com/opencontainers/image-spec/blob/main/descriptor.md.
type Descriptor struct {
	MediaType   string            `json:"mediaType,omitempty"`
	Digest      string            `json:"digest"`
	Size        int64             `json:"size"`
	URLs        []string          `json:"urls,omitempty"`
	Annotations map[string]string `json:"annotations,omitempty"`
	// Platform appears on child descriptors of an image index.
	Platform *Platform `json:"platform,omitempty"`
}

// Platform describes the platform an image target is suitable for.
type Platform struct {
	Architecture string   `json:"architecture"`
	OS           string   `json:"os"`
	OSVersion    string   `json:"os.version,omitempty"`
	OSFeatures   []string `json:"os.features,omitempty"`
	Variant      string   `json:"variant,omitempty"`
}

// Index is an OCI image index:
// https://github.com/opencontainers/image-spec/blob/main/image-index.md
type Index struct {
	SchemaVersion int               `json:"schemaVersion"`
	MediaType     string            `json:"mediaType,omitempty"`
	Manifests     []Descriptor      `json:"manifests"`
	Annotations   map[string]string `json:"annotations,omitempty"`
}

// Manifest is an OCI image manifest:
// https://github.com/opencontainers/image-spec/blob/main/manifest.md
type Manifest struct {
	SchemaVersion int               `json:"schemaVersion"`
	MediaType     string            `json:"mediaType,omitempty"`
	Config        Descriptor        `json:"config"`
	Layers        []Descriptor      `json:"layers"`
	Annotations   map[string]string `json:"annotations,omitempty"`
}

// ImageConfig is the "image config" blob of an image manifest:
// https://github.com/opencontainers/image-spec/blob/main/config.md
type ImageConfig struct {
	OS           string `json:"os"`
	Architecture string `json:"architecture"`
	Variant      string `json:"variant,omitempty"`
	OSVersion    string `json:"os.version,omitempty"`

	// RootFS and the remaining config fields are carried verbatim but not
	// needed for platform selection.
	RootFS *RootFS `json:"rootfs,omitempty"`
}

// RootFS is the rootfs section of an image config.
type RootFS struct {
	Type    string   `json:"type"`
	DiffIDs []string `json:"diff_ids"`
}

// AnnotationRefName is the OCI-layout index.json annotation that binds a
// descriptor to a tag/reference name.
const AnnotationRefName = "org.opencontainers.image.ref.name"

// LayoutFile is the marker file marking a directory as an OCI image layout.
const LayoutFile = "oci-layout"

// LayoutVersion is the supported OCI image-layout version.
const LayoutVersion = "1.0.0"
