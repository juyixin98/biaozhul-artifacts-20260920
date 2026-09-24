package ledger

import (
	"fmt"

	"k8s.io/apimachinery/pkg/types"

	quota "resourcequota-reservation/api/v1alpha1"
)

// ErrInsufficientCapacity is returned when a debit would exceed the pool.
type ErrInsufficientCapacity struct {
	CPUCap, CPUUsed, CPUWant int64
	MemCap, MemUsed, MemWant int64
}

func (e *ErrInsufficientCapacity) Error() string {
	return fmt.Sprintf("insufficient capacity: cpu used=%dm/%dm want=%dm; memory used=%d/%d want=%d",
		e.CPUUsed, e.CPUCap, e.CPUWant, e.MemUsed, e.MemCap, e.MemWant)
}

// EnsureDebited idempotently records a claim's contribution in the pool status.
//
// It is a pure function over PoolStatus so the controller can call it inside a
// retry loop and submit the result with an optimistic Update. Idempotency key
// is the claim UID: repeated reconciles for the same claim never debit twice,
// even after controller restarts, because the decision is durable in the pool
// object — not held in process memory.
//
// Returns a bool indicating whether the totals actually changed (false when
// the claim was already recorded with identical values).
func EnsureDebited(pool *quota.ReservationPool, uid types.UID, name string, cpu, mem int64) (bool, error) {
	cpuCap, err := ParseCPU(pool.Spec.CPUCapacity)
	if err != nil {
		return false, fmt.Errorf("pool spec invalid cpuCapacity: %w", err)
	}
	memCap, err := ParseMemory(pool.Spec.MemoryCapacity)
	if err != nil {
		return false, fmt.Errorf("pool spec invalid memoryCapacity: %w", err)
	}
	if pool.Status.Claims == nil {
		pool.Status.Claims = map[string]quota.PoolClaimEntry{}
	}

	usedCPU, usedMem := totals(pool.Status.Claims)

	if existing, ok := pool.Status.Claims[string(uid)]; ok {
		// Already debited. Claim specs are immutable (webhook-enforced), so a
		// changed amount indicates a programming error; we still refuse to
		// double-charge.
		if existing.CPU == cpu && existing.Memory == mem && existing.Name == name && existing.Active {
			return false, nil
		}
		// Otherwise the entry was previously credited (Expired) and is being
		// re-debited through the late-pod recovery path; fall through and
		// re-insert it after the capacity check.
	}

	// Capacity check happens *against the candidate totals*, atomically with
	// recording the entry in the same Update call. The API server rejects
	// concurrent updates to the pool whose resourceVersion went stale, so two
	// controllers can never both pass this check on the same base totals.
	if usedCPU+cpu > cpuCap || usedMem+mem > memCap {
		return false, &ErrInsufficientCapacity{
			CPUCap: cpuCap, CPUUsed: usedCPU, CPUWant: cpu,
			MemCap: memCap, MemUsed: usedMem, MemWant: mem,
		}
	}

	pool.Status.Claims[string(uid)] = quota.PoolClaimEntry{
		Name:   name,
		CPU:    cpu,
		Memory: mem,
		Active: true,
	}
	recomputeTotals(pool)
	return true, nil
}

// Credit idempotently removes a claim's contribution (used on expiry and on
// claim deletion). Returns true if totals changed.
func Credit(pool *quota.ReservationPool, uid types.UID) bool {
	entry, ok := pool.Status.Claims[string(uid)]
	if !ok || !entry.Active {
		return false
	}
	entry.Active = false
	delete(pool.Status.Claims, string(uid))
	recomputeTotals(pool)
	return true
}

// MarkInactive flips an entry inactive but keeps it around; currently unused
// externally but part of the ledger API for the Bound->deleted flow.
func MarkInactive(pool *quota.ReservationPool, uid types.UID) bool {
	entry, ok := pool.Status.Claims[string(uid)]
	if !ok || !entry.Active {
		return false
	}
	entry.Active = false
	pool.Status.Claims[string(uid)] = entry
	recomputeTotals(pool)
	return true
}

func totals(claims map[string]quota.PoolClaimEntry) (cpu, mem int64) {
	for _, e := range claims {
		if e.Active {
			cpu += e.CPU
			mem += e.Memory
		}
	}
	return cpu, mem
}

func recomputeTotals(pool *quota.ReservationPool) {
	cpu, mem := totals(pool.Status.Claims)
	pool.Status.ReservedCPU = FormatMilliCPU(cpu)
	pool.Status.ReservedMemory = FormatBytes(mem)
}

// Used returns the current totals (for tests and logging).
func Used(pool *quota.ReservationPool) (cpu, mem int64) {
	return totals(pool.Status.Claims)
}
