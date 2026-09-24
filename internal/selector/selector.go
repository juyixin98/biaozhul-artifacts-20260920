// Package selector implements exact OCI platform selection.
//
// Selection rules (explicit, documented — never randomized):
//
//  1. os and architecture must match exactly.
//  2. If the request specifies a non-empty variant, the candidate variant
//     must equal it exactly.
//  3. If the request leaves variant empty:
//     a. a candidate whose variant is also empty is preferred ("no variant
//     requested, no variant declared");
//     b. otherwise the architecture's documented default variant is applied
//     (arm -> v7, arm64 -> v8);
//     c. architectures with no documented default require an empty variant.
//  4. When exactly one candidate matches it is selected. When two or more
//     distinct candidates match the platform, the result is ambiguous and the
//     caller must reject the request instead of guessing.
package selector

import (
	"fmt"
	"sort"
	"strings"

	"github.example.com/ocimultipick/internal/oci"
)

// Platform is the requested platform.
type Platform struct {
	OS      string `json:"os"`
	Arch    string `json:"architecture"`
	Variant string `json:"variant,omitempty"`
}

func (p Platform) String() string {
	s := p.OS + "/" + p.Arch
	if p.Variant != "" {
		s += "/" + p.Variant
	}
	return s
}

// defaultVariants maps an architecture to the variant assumed when the client
// leaves variant empty and the index only declares variant-qualified entries.
// Values follow the historical ARM meaning used by the OCI ecosystem:
// "arm" without qualification means ARMv7, "arm64" without qualification
// means ARMv8.
var defaultVariants = map[string]string{
	"arm":   "v7",
	"arm64": "v8",
}

// AmbiguousError is returned when more than one candidate matches.
type AmbiguousError struct {
	Platform   Platform
	Candidates []oci.Descriptor
}

func (e *AmbiguousError) Error() string {
	digests := make([]string, 0, len(e.Candidates))
	for _, c := range e.Candidates {
		digests = append(digests, c.Digest)
	}
	sort.Strings(digests)
	return fmt.Sprintf("platform %s is ambiguous: %d candidates: %s",
		e.Platform, len(e.Candidates), strings.Join(digests, ", "))
}

// validateRequest checks that the requested platform is well-formed.
func validateRequest(p Platform) error {
	if p.OS == "" {
		return fmt.Errorf("platform selection requires os")
	}
	if p.Arch == "" {
		return fmt.Errorf("platform selection requires architecture")
	}
	return nil
}

// Select picks exactly one candidate manifest descriptor for p.
func Select(p Platform, candidates []oci.Descriptor) (oci.Descriptor, error) {
	if err := validateRequest(p); err != nil {
		return oci.Descriptor{}, err
	}

	// Phase 1: same os and architecture.
	var sameOSArch []oci.Descriptor
	for _, c := range candidates {
		if c.Platform == nil {
			continue
		}
		if c.Platform.OS == p.OS && c.Platform.Architecture == p.Arch {
			sameOSArch = append(sameOSArch, c)
		}
	}
	if len(sameOSArch) == 0 {
		return oci.Descriptor{}, &NoMatchError{Platform: p, Reason: "no manifest declares os/architecture " + p.String()}
	}

	// Phase 2: variant matching.
	var matched []oci.Descriptor
	if p.Variant != "" {
		// Explicit variant: exact match only.
		for _, c := range sameOSArch {
			if c.Platform.Variant == p.Variant {
				matched = append(matched, c)
			}
		}
	} else {
		// No requested variant: a declared-empty variant wins outright.
		for _, c := range sameOSArch {
			if c.Platform.Variant == "" {
				matched = append(matched, c)
			}
		}
		// Otherwise expand the documented default for this architecture.
		if len(matched) == 0 {
			def, hasDefault := defaultVariants[p.Arch]
			if hasDefault {
				for _, c := range sameOSArch {
					if c.Platform.Variant == def {
						matched = append(matched, c)
					}
				}
			}
		}
	}

	matched = dedupe(matched)
	switch len(matched) {
	case 0:
		return oci.Descriptor{}, &NoMatchError{
			Platform: p,
			Reason:   fmt.Sprintf("no manifest matches the variant rule for %s", p),
		}
	case 1:
		return matched[0], nil
	default:
		return oci.Descriptor{}, &AmbiguousError{Platform: p, Candidates: matched}
	}
}

// NoMatchError indicates no candidate fits the requested platform.
type NoMatchError struct {
	Platform Platform
	Reason   string
}

func (e *NoMatchError) Error() string {
	return fmt.Sprintf("no match for platform %s: %s", e.Platform, e.Reason)
}

// dedupe removes descriptors pointing at the same digest. Two descriptors with
// the same digest are the same artifact and must not create false ambiguity.
func dedupe(in []oci.Descriptor) []oci.Descriptor {
	seen := make(map[string]bool, len(in))
	out := make([]oci.Descriptor, 0, len(in))
	for _, d := range in {
		if seen[d.Digest] {
			continue
		}
		seen[d.Digest] = true
		out = append(out, d)
	}
	return out
}
