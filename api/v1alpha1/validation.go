package v1alpha1

import (
	"fmt"

	"k8s.io/apimachinery/pkg/util/validation/field"
)

// Per-request limits enforced by the validating webhook. These are the
// single source of truth for "units and upper bounds" validation; the CRD
// schema markers mirror them as defense in depth.
const (
	// MaxCPUMilli is the per-request CPU ceiling (100 cores).
	MaxCPUMilli int64 = 100000
	// MaxMemoryBytes is the per-request memory ceiling (100 GiB).
	MaxMemoryBytes int64 = 100 << 30
	// MinTTLSeconds is the shortest allowed reservation TTL.
	MinTTLSeconds int64 = 10
	// MaxTTLSeconds is the longest allowed reservation TTL (24h).
	MaxTTLSeconds int64 = 86400
)

// ValidateResourceRequestSpec checks units and upper bounds of a spec.
// It returns a field.ErrorList suitable for apierrors.NewInvalid.
func ValidateResourceRequestSpec(spec *ResourceRequestSpec) field.ErrorList {
	allErrs := field.ErrorList{}
	specPath := field.NewPath("spec")

	if spec.CPUMilli < 1 {
		allErrs = append(allErrs, field.Invalid(
			specPath.Child("cpuMilli"), spec.CPUMilli,
			"must be a positive integer of milli-cores (e.g. 500 = 0.5 cores)"))
	}
	if spec.CPUMilli > MaxCPUMilli {
		allErrs = append(allErrs, field.Invalid(
			specPath.Child("cpuMilli"), spec.CPUMilli,
			fmt.Sprintf("exceeds the per-request limit of %d milli-cores", MaxCPUMilli)))
	}

	if spec.MemoryBytes < 1 {
		allErrs = append(allErrs, field.Invalid(
			specPath.Child("memoryBytes"), spec.MemoryBytes,
			"must be a positive integer of bytes"))
	}
	if spec.MemoryBytes > MaxMemoryBytes {
		allErrs = append(allErrs, field.Invalid(
			specPath.Child("memoryBytes"), spec.MemoryBytes,
			fmt.Sprintf("exceeds the per-request limit of %d bytes (%d GiB)", MaxMemoryBytes, MaxMemoryBytes>>30)))
	}

	if spec.TTLSeconds < MinTTLSeconds {
		allErrs = append(allErrs, field.Invalid(
			specPath.Child("ttlSeconds"), spec.TTLSeconds,
			fmt.Sprintf("must be at least %d seconds", MinTTLSeconds)))
	}
	if spec.TTLSeconds > MaxTTLSeconds {
		allErrs = append(allErrs, field.Invalid(
			specPath.Child("ttlSeconds"), spec.TTLSeconds,
			fmt.Sprintf("exceeds the maximum of %d seconds (24h)", MaxTTLSeconds)))
	}

	return allErrs
}
