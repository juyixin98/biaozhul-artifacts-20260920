package oci

import (
	"errors"
	"testing"
)

func desc(os, arch, variant, digest string) Descriptor {
	return Descriptor{
		MediaType: MediaTypeManifestOCI,
		Digest:    digest,
		Size:      1,
		Platform:  &Platform{OS: os, Architecture: arch, Variant: variant},
	}
}

func TestSelectExactMatch(t *testing.T) {
	descs := []Descriptor{
		desc("linux", "amd64", "", "sha256:aaa"),
		desc("linux", "arm64", "", "sha256:bbb"),
	}
	got, err := Select(descs, Platform{OS: "linux", Architecture: "amd64"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Digest != "sha256:aaa" {
		t.Fatalf("want sha256:aaa, got %s", got.Digest)
	}
}

func TestSelectVariantExact(t *testing.T) {
	descs := []Descriptor{
		desc("linux", "arm", "v7", "sha256:v7"),
		desc("linux", "arm", "v8", "sha256:v8"),
	}
	got, err := Select(descs, Platform{OS: "linux", Architecture: "arm", Variant: "v8"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Digest != "sha256:v8" {
		t.Fatalf("want sha256:v8, got %s", got.Digest)
	}
}

func TestSelectNoVariantPrefersVariantless(t *testing.T) {
	descs := []Descriptor{
		desc("linux", "arm", "v7", "sha256:v7"),
		desc("linux", "arm", "", "sha256:plain"),
	}
	got, err := Select(descs, Platform{OS: "linux", Architecture: "arm"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Digest != "sha256:plain" {
		t.Fatalf("want sha256:plain, got %s", got.Digest)
	}
}

func TestSelectNoVariantFallbackThenAmbiguous(t *testing.T) {
	// No variant-less manifest exists: both v7 and v8 compete -> ambiguity,
	// never a random pick.
	descs := []Descriptor{
		desc("linux", "arm", "v7", "sha256:v7"),
		desc("linux", "arm", "v8", "sha256:v8"),
	}
	_, err := Select(descs, Platform{OS: "linux", Architecture: "arm"})
	var amb *AmbiguousError
	if !errors.As(err, &amb) {
		t.Fatalf("want AmbiguousError, got %v", err)
	}
	if len(amb.Candidates) != 2 {
		t.Fatalf("want 2 candidates, got %d", len(amb.Candidates))
	}
}

func TestSelectAmbiguousSamePlatform(t *testing.T) {
	descs := []Descriptor{
		desc("linux", "amd64", "", "sha256:a"),
		desc("linux", "amd64", "", "sha256:b"),
	}
	_, err := Select(descs, Platform{OS: "linux", Architecture: "amd64"})
	var amb *AmbiguousError
	if !errors.As(err, &amb) {
		t.Fatalf("want AmbiguousError, got %v", err)
	}
}

func TestSelectNoMatch(t *testing.T) {
	descs := []Descriptor{desc("linux", "amd64", "", "sha256:a")}
	_, err := Select(descs, Platform{OS: "windows", Architecture: "amd64"})
	if !errors.Is(err, ErrNoMatch) {
		t.Fatalf("want ErrNoMatch, got %v", err)
	}
}

func TestSelectRequestedVariantMissing(t *testing.T) {
	descs := []Descriptor{desc("linux", "arm", "v7", "sha256:v7")}
	_, err := Select(descs, Platform{OS: "linux", Architecture: "arm", Variant: "v6"})
	if !errors.Is(err, ErrNoMatch) {
		t.Fatalf("want ErrNoMatch, got %v", err)
	}
}
