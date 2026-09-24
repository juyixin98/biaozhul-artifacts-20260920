// Package ledger implements the capacity bookkeeping primitives shared by the
// controller and the webhook.
//
// All arithmetic here is integer-based: CPU in milli-cores, memory in bytes.
// Quantities are parsed with the Kubernetes API machinery parser
// (resource.Quantity), which is the exact same code that validates Pod
// requests — webhook and controller therefore can never disagree on units.
package ledger

import (
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	quota "resourcequota-reservation/api/v1alpha1"
)

// Limits enforced by the validating webhook.
const (
	MinMilliCPU int64 = 1
	MaxMilliCPU int64 = 64_000 // 64 cores
	MinMemBytes int64 = 1 * 1024 * 1024
	MaxMemBytes int64 = 256 * 1024 * 1024 * 1024
)

// ParseCPU parses a CPU quantity ("500m", "2") into milli-cores.
// Both whole cores ("1" -> 1000) and fractional ("0.5", "500m") are accepted;
// the result is always an integral number of milli-cores (sub-milli precision
// is rejected).
func ParseCPU(s string) (int64, error) {
	q, err := resource.ParseQuantity(s)
	if err != nil {
		return 0, fmt.Errorf("invalid cpu quantity %q: %w", s, err)
	}
	if q.Sign() < 0 {
		return 0, fmt.Errorf("cpu quantity %q must not be negative", s)
	}
	milli := q.ScaledValue(resource.Milli)
	if milli == 0 && !q.IsZero() {
		return 0, fmt.Errorf("cpu quantity %q is below 1m precision", s)
	}
	return milli, nil
}

// ParseMemory parses a memory quantity ("128Mi", "1Gi") into bytes.
func ParseMemory(s string) (int64, error) {
	q, err := resource.ParseQuantity(s)
	if err != nil {
		return 0, fmt.Errorf("invalid memory quantity %q: %w", s, err)
	}
	if q.Sign() < 0 {
		return 0, fmt.Errorf("memory quantity %q must not be negative", s)
	}
	b, ok := q.AsInt64()
	if !ok {
		return 0, fmt.Errorf("memory quantity %q out of int64 range", s)
	}
	return b, nil
}

// ValidateClaimSpec validates units and bounds the same way the webhook does;
// exposed so the controller/integration tests share one implementation.
func ValidateClaimSpec(spec quota.ResourceClaimSpec) (cpu, mem int64, err error) {
	cpu, err = ParseCPU(spec.CPU)
	if err != nil {
		return 0, 0, err
	}
	if cpu < MinMilliCPU || cpu > MaxMilliCPU {
		return 0, 0, fmt.Errorf("cpu %dm outside allowed range [%dm,%dm]", cpu, MinMilliCPU, MaxMilliCPU)
	}
	mem, err = ParseMemory(spec.Memory)
	if err != nil {
		return 0, 0, err
	}
	if mem < MinMemBytes || mem > MaxMemBytes {
		return 0, 0, fmt.Errorf("memory %d outside allowed range [%d,%d] bytes", mem, MinMemBytes, MaxMemBytes)
	}
	if spec.TTL.Duration <= 0 {
		return 0, 0, errors.New("ttl must be positive")
	}
	if spec.TTL.Duration > 24*60*60*1_000_000_000 {
		return 0, 0, errors.New("ttl must be <= 24h")
	}
	return cpu, mem, nil
}

// FormatMilliCPU renders integer milli-cores as a canonical quantity string:
// multiples of 1000 render as whole cores ("2"), otherwise with "m" ("1500m").
func FormatMilliCPU(milli int64) string {
	return resource.NewMilliQuantity(milli, resource.DecimalSI).String()
}

// FormatBytes renders integer bytes.
func FormatBytes(b int64) string {
	return resource.NewQuantity(b, resource.BinarySI).String()
}

// PodRequests extracts the declared cpu(milli)/memory(bytes) requests of a Pod's
// init+containers, taking the max over containers per resource like scheduling
// does for non-overhead resources. For simplicity (and conservatively) we sum
// sidecar-less regular containers, which is what the samples use.
func PodRequests(pod *corev1.Pod) (milliCPU, memBytes int64) {
	add := func(rl corev1.ResourceList) {
		if c, ok := rl[corev1.ResourceCPU]; ok {
			milliCPU += c.ScaledValue(resource.Milli)
		}
		if m, ok := rl[corev1.ResourceMemory]; ok {
			if b, ok := m.AsInt64(); ok {
				memBytes += b
			}
		}
	}
	for i := range pod.Spec.Containers {
		add(pod.Spec.Containers[i].Resources.Requests)
	}
	for i := range pod.Spec.InitContainers {
		// Init containers run sequentially; take the max, not the sum.
		var icpu, imem int64
		c := pod.Spec.InitContainers[i].Resources.Requests
		if q, ok := c[corev1.ResourceCPU]; ok {
			icpu = q.ScaledValue(resource.Milli)
		}
		if q, ok := c[corev1.ResourceMemory]; ok {
			imem, _ = q.AsInt64()
		}
		if icpu > milliCPU {
			milliCPU = icpu
		}
		if imem > memBytes {
			memBytes = imem
		}
	}
	return milliCPU, memBytes
}
