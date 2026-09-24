// Package reserve implements the quota ledger math as pure functions, so the
// concurrency-critical accounting is unit-testable without a cluster.
//
// Concurrency model: callers do NOT hold a lock. They read a QuotaPool, run
// these functions against its status, and write the status back with
// resourceVersion-checked Update, retrying on conflict (optimistic
// concurrency). The ledger is keyed by ResourceRequest UID, which makes
// Reserve idempotent: a retried reconcile of the same request can never
// double-count.
package reserve

import (
	"errors"
	"fmt"

	"k8s.io/apimachinery/pkg/types"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
)

// ErrExceedsCapacity is returned when a reservation would exceed pool capacity.
var ErrExceedsCapacity = errors.New("insufficient pool capacity")

// Sum recomputes aggregate usage from the allocation ledger.
func Sum(allocs []quotav1alpha1.Allocation) (cpuMilli, memoryBytes int64) {
	for _, a := range allocs {
		cpuMilli += a.CPUMilli
		memoryBytes += a.MemoryBytes
	}
	return cpuMilli, memoryBytes
}

// Has reports whether the ledger already holds an allocation for uid.
func Has(allocs []quotav1alpha1.Allocation, uid types.UID) bool {
	for _, a := range allocs {
		if a.RequestUID == uid {
			return true
		}
	}
	return false
}

// Reserve adds (uid -> cpu/mem) to the ledger if capacity allows.
//
// Idempotency: if the ledger already holds uid, nothing changes and
// already=true is returned — a retried reconcile never double-counts.
// Used totals are always recomputed from the ledger (self-healing).
func Reserve(st *quotav1alpha1.QuotaPoolStatus, spec quotav1alpha1.QuotaPoolSpec,
	uid types.UID, name string, cpuMilli, memoryBytes int64) (already bool, err error) {

	if Has(st.Allocations, uid) {
		recompute(st)
		return true, nil
	}
	usedCPU, usedMem := Sum(st.Allocations)
	if usedCPU+cpuMilli > spec.CPUMilli || usedMem+memoryBytes > spec.MemoryBytes {
		recompute(st)
		return false, fmt.Errorf("%w: request cpu=%dm mem=%dB, used cpu=%dm/%dm mem=%dB/%dB",
			ErrExceedsCapacity, cpuMilli, memoryBytes, usedCPU, spec.CPUMilli, usedMem, spec.MemoryBytes)
	}
	st.Allocations = append(st.Allocations, quotav1alpha1.Allocation{
		RequestUID:  uid,
		RequestName: name,
		CPUMilli:    cpuMilli,
		MemoryBytes: memoryBytes,
	})
	recompute(st)
	return false, nil
}

// Release removes uid from the ledger. It returns true if an entry was
// removed; releasing an unknown uid is a no-op (idempotent).
func Release(st *quotav1alpha1.QuotaPoolStatus, uid types.UID) bool {
	kept := st.Allocations[:0]
	released := false
	for _, a := range st.Allocations {
		if a.RequestUID == uid {
			released = true
			continue
		}
		kept = append(kept, a)
	}
	if !released {
		return false
	}
	// Clear the tail so dropped entries are not retained by the backing array.
	for i := len(kept); i < len(st.Allocations); i++ {
		st.Allocations[i] = quotav1alpha1.Allocation{}
	}
	st.Allocations = kept
	recompute(st)
	return true
}

// recompute derives the Used counters from the ledger.
func recompute(st *quotav1alpha1.QuotaPoolStatus) {
	st.UsedCPUMilli, st.UsedMemoryBytes = Sum(st.Allocations)
}
