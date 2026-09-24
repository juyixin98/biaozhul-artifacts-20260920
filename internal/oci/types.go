// Package oci implements local OCI image-layout parsing, digest/size
// verification and multi-architecture platform selection.
package oci

// Media types we understand (OCI + Docker v2 equivalents).
const (
	MediaTypeIndexOCI     = "application/vnd.oci.image.index.v1+json"
	MediaTypeIndexDocker  = "application/vnd.docker.distribution.manifest.list.v2+json"
	MediaTypeManifestOCI  = "application/vnd.oci.image.manifest.v1+json"
	MediaTypeManifestDock = "application/vnd.docker.distribution.manifest.v2+json"
	MediaTypeConfigOCI    = "application/vnd.oci.image.config.v1+json"
	MediaTypeConfigDocker = "application/vnd.docker.container.image.v1+json"
)

// Platform is the os/architecture/variant triple used for selection.
type Platform struct {
	OS           string `json:"os"`
	Architecture string `json:"architecture"`
	Variant      string `json:"variant,omitempty"`
}

// Descriptor references a blob by digest and size.
type Descriptor struct {
	MediaType string    `json:"mediaType"`
	Digest    string    `json:"digest"`
	Size      int64     `json:"size"`
	Platform  *Platform `json:"platform,omitempty"`
}

// Index is an OCI image index (fat manifest).
type Index struct {
	MediaType string       `json:"mediaType"`
	Manifests []Descriptor `json:"manifests"`
}

// Manifest is a single-platform image manifest.
type Manifest struct {
	MediaType string       `json:"mediaType"`
	Config    Descriptor   `json:"config"`
	Layers    []Descriptor `json:"layers"`
}

// Config is the image config document carrying the real platform.
type Config struct {
	OS           string `json:"os"`
	Architecture string `json:"architecture"`
	Variant      string `json:"variant,omitempty"`
}

func IsIndexMediaType(mt string) bool {
	return mt == MediaTypeIndexOCI || mt == MediaTypeIndexDocker
}

func IsManifestMediaType(mt string) bool {
	return mt == MediaTypeManifestOCI || mt == MediaTypeManifestDock
}
