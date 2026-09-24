package oci

import (
	"errors"
	"fmt"
	"strings"
)

// ErrNoMatch is returned when no manifest satisfies the requested platform.
var ErrNoMatch = errors.New("no manifest matches the requested platform")

// AmbiguousError is returned when more than one manifest satisfies the
// request. Selection is never resolved by picking one at random.
type AmbiguousError struct {
	Platform   Platform
	Candidates []Descriptor
}

func (e *AmbiguousError) Error() string {
	parts := make([]string, 0, len(e.Candidates))
	for _, c := range e.Candidates {
		v := ""
		if c.Platform != nil && c.Platform.Variant != "" {
			v = " variant=" + c.Platform.Variant
		}
		parts = append(parts, c.Digest+v)
	}
	return fmt.Sprintf("ambiguous platform %s/%s variant=%q: %d candidates: %s",
		e.Platform.OS, e.Platform.Architecture, e.Platform.Variant,
		len(e.Candidates), strings.Join(parts, ", "))
}

// Select picks exactly one manifest descriptor for the requested platform.
//
// Explicit rules (documented in README):
//  1. Keep descriptors whose platform os AND architecture match exactly.
//  2. Variant: if the request names a variant, keep only exact variant
//     matches. If the request omits variant, prefer manifests that also
//     carry no variant; only when none exists, fall back to all os/arch
//     matches regardless of variant.
//  3. Zero candidates  -> ErrNoMatch.
//     More than one    -> *AmbiguousError (never a random pick).
//     Exactly one      -> selected.
func Select(descs []Descriptor, want Platform) (Descriptor, error) {
	var osArch []Descriptor
	for _, d := range descs {
		if d.Platform == nil {
			continue
		}
		if d.Platform.OS == want.OS && d.Platform.Architecture == want.Architecture {
			osArch = append(osArch, d)
		}
	}

	var pool []Descriptor
	if want.Variant != "" {
		for _, d := range osArch {
			if d.Platform.Variant == want.Variant {
				pool = append(pool, d)
			}
		}
	} else {
		for _, d := range osArch {
			if d.Platform.Variant == "" {
				pool = append(pool, d)
			}
		}
		if len(pool) == 0 {
			// Documented fallback: no variant-less manifest exists, so all
			// os/arch matches compete; ties are reported as ambiguous.
			pool = osArch
		}
	}

	switch len(pool) {
	case 0:
		return Descriptor{}, fmt.Errorf("%w: os=%s architecture=%s variant=%q",
			ErrNoMatch, want.OS, want.Architecture, want.Variant)
	case 1:
		return pool[0], nil
	default:
		return Descriptor{}, &AmbiguousError{Platform: want, Candidates: pool}
	}
}
