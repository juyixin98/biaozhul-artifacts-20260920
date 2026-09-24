package selector

import (
	"errors"
	"testing"

	"github.example.com/ocimultipick/internal/oci"
)

func desc(dgst, goos, arch, variant string) oci.Descriptor {
	return oci.Descriptor{
		Digest:   dgst,
		Platform: &oci.Platform{OS: goos, Architecture: arch, Variant: variant},
	}
}

func TestExactOSArch(t *testing.T) {
	cands := []oci.Descriptor{
		desc("sha256:aaa1", "linux", "amd64", ""),
		desc("sha256:bbb1", "linux", "arm64", ""),
	}
	got, err := Select(Platform{OS: "linux", Arch: "arm64"}, cands)
	if err != nil {
		t.Fatal(err)
	}
	if got.Digest != "sha256:bbb1" {
		t.Fatalf("selected %s, want arm64 candidate", got.Digest)
	}
}

func TestExplicitVariantMustMatchExactly(t *testing.T) {
	cands := []oci.Descriptor{
		desc("sha256:v6", "linux", "arm", "v6"),
		desc("sha256:v7", "linux", "arm", "v7"),
	}
	got, err := Select(Platform{OS: "linux", Arch: "arm", Variant: "v6"}, cands)
	if err != nil {
		t.Fatal(err)
	}
	if got.Digest != "sha256:v6" {
		t.Fatalf("selected %s, want v6", got.Digest)
	}

	// A variant that exists for no candidate is a hard miss, never a fallback.
	if _, err := Select(Platform{OS: "linux", Arch: "arm", Variant: "v5"}, cands); err == nil {
		t.Fatal("expected no-match for variant v5, got success")
	} else if !errors.As(err, new(*NoMatchError)) {
		t.Fatalf("expected NoMatchError, got %T %v", err, err)
	}
}

func TestEmptyVariantPrefersEmpty(t *testing.T) {
	// Both an empty-variant and v7 entry exist for linux/arm: asking without a
	// variant must pick the empty one, not the default-expanded v7.
	cands := []oci.Descriptor{
		desc("sha256:empty", "linux", "arm", ""),
		desc("sha256:v7", "linux", "arm", "v7"),
	}
	got, err := Select(Platform{OS: "linux", Arch: "arm"}, cands)
	if err != nil {
		t.Fatal(err)
	}
	if got.Digest != "sha256:empty" {
		t.Fatalf("selected %s, want the empty-variant candidate", got.Digest)
	}
}

func TestEmptyVariantDefaultsArmV7(t *testing.T) {
	cands := []oci.Descriptor{
		desc("sha256:v6", "linux", "arm", "v6"),
		desc("sha256:v7", "linux", "arm", "v7"),
	}
	got, err := Select(Platform{OS: "linux", Arch: "arm"}, cands)
	if err != nil {
		t.Fatal(err)
	}
	if got.Digest != "sha256:v7" {
		t.Fatalf("selected %s, want default arm -> v7", got.Digest)
	}
}

func TestEmptyVariantDefaultsArm64V8(t *testing.T) {
	cands := []oci.Descriptor{
		desc("sha256:v8", "linux", "arm64", "v8"),
	}
	got, err := Select(Platform{OS: "linux", Arch: "arm64"}, cands)
	if err != nil {
		t.Fatal(err)
	}
	if got.Digest != "sha256:v8" {
		t.Fatalf("selected %s, want default arm64 -> v8", got.Digest)
	}
}

func TestNoDefaultForUnknownArchRequiresEmptyVariant(t *testing.T) {
	cands := []oci.Descriptor{
		desc("sha256:x", "linux", "mips", "something"),
	}
	if _, err := Select(Platform{OS: "linux", Arch: "mips"}, cands); !errors.As(err, new(*NoMatchError)) {
		t.Fatalf("expected NoMatchError for arch without documented default, got %v", err)
	}
}

func TestAmbiguousDistinctCandidates(t *testing.T) {
	cands := []oci.Descriptor{
		desc("sha256:alpha", "linux", "amd64", ""),
		desc("sha256:beta", "linux", "amd64", ""),
	}
	_, err := Select(Platform{OS: "linux", Arch: "amd64"}, cands)
	var amb *AmbiguousError
	if !errors.As(err, &amb) {
		t.Fatalf("expected AmbiguousError, got %T %v", err, err)
	}
	if len(amb.Candidates) != 2 {
		t.Fatalf("ambiguity lists %d candidates, want 2", len(amb.Candidates))
	}
}

func TestDuplicateSameDigestIsNotAmbiguous(t *testing.T) {
	// Two descriptors listing the same digest are the same artifact (common in
	// nested indexes); they must collapse to one result.
	cands := []oci.Descriptor{
		desc("sha256:same", "linux", "amd64", ""),
		desc("sha256:same", "linux", "amd64", ""),
	}
	got, err := Select(Platform{OS: "linux", Arch: "amd64"}, cands)
	if err != nil {
		t.Fatalf("identical-digest entries should not be ambiguous: %v", err)
	}
	if got.Digest != "sha256:same" {
		t.Fatalf("selected %s", got.Digest)
	}
}

func TestNoMatch(t *testing.T) {
	cands := []oci.Descriptor{desc("sha256:x", "linux", "amd64", "")}
	if _, err := Select(Platform{OS: "windows", Arch: "amd64"}, cands); !errors.As(err, new(*NoMatchError)) {
		t.Fatalf("expected NoMatchError, got %v", err)
	}
}

func TestRequiresOSAndArch(t *testing.T) {
	if _, err := Select(Platform{Arch: "amd64"}, nil); err == nil {
		t.Fatal("expected error for missing os")
	}
	if _, err := Select(Platform{OS: "linux"}, nil); err == nil {
		t.Fatal("expected error for missing architecture")
	}
}
