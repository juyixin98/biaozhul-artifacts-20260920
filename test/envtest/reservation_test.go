package envtest

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
)

// TestConcurrentReservation hammers one pool with 20 concurrent requests for
// 150m/128Mi each against 1000m/1Gi. Exactly 6 must win (6*150m=900m fits,
// 7*150m=1050m does not), 14 must be Rejected, and the pool ledger must match
// the winning requests exactly — proving optimistic concurrency prevents
// over-commit under real resourceVersion contention.
func TestConcurrentReservation(t *testing.T) {
	ns := newNamespace(t, false)
	createPool(t, ns, 1000, 1<<30)
	startManager(t, false)

	const n = 20
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rr := &quotav1alpha1.ResourceRequest{}
			rr.Name = fmt.Sprintf("rr-%02d", i)
			rr.Namespace = ns
			rr.Spec.Pool = "default"
			rr.Spec.CPUMilli = 150
			rr.Spec.MemoryBytes = 128 << 20
			rr.Spec.TTLSeconds = 600
			if err := k8sClient.Create(context.Background(), rr); err != nil {
				t.Errorf("creating rr-%02d: %v", i, err)
			}
		}(i)
	}
	wg.Wait()

	eventually(t, 60*time.Second, "all requests to leave Pending", func() bool {
		counts := countPhases(t, ns)
		return counts[quotav1alpha1.PhaseReserved]+counts[quotav1alpha1.PhaseRejected] == n
	})

	counts := countPhases(t, ns)
	if counts[quotav1alpha1.PhaseReserved] != 6 || counts[quotav1alpha1.PhaseRejected] != 14 {
		t.Fatalf("phases = %v, want 6 Reserved + 14 Rejected", counts)
	}
	pool := getPool(t, ns)
	if pool.Status.UsedCPUMilli != 900 || pool.Status.UsedMemoryBytes != 6*128<<20 {
		t.Fatalf("pool used = %dm/%dB, want 900m/%dB",
			pool.Status.UsedCPUMilli, pool.Status.UsedMemoryBytes, 6*128<<20)
	}
	if len(pool.Status.Allocations) != 6 {
		t.Fatalf("ledger has %d entries, want 6", len(pool.Status.Allocations))
	}
	checkInvariant(t, ns)

	// Deleting all requests must drain the pool via the finalizer.
	var list quotav1alpha1.ResourceRequestList
	if err := k8sClient.List(context.Background(), &list); err != nil {
		t.Fatal(err)
	}
	for i := range list.Items {
		if err := k8sClient.Delete(context.Background(), &list.Items[i]); err != nil {
			t.Fatal(err)
		}
	}
	eventually(t, 30*time.Second, "pool to drain after deleting requests", func() bool {
		return getPool(t, ns).Status.UsedCPUMilli == 0
	})
	checkInvariant(t, ns)
}

// TestDuplicateReconcileNoDoubleCount simulates the controller crashing
// between the pool-ledger update and the request status update: the ledger
// holds the allocation but the request still says Pending. The next reconcile
// must reach Reserved without double-counting.
func TestDuplicateReconcileNoDoubleCount(t *testing.T) {
	ns := newNamespace(t, false)
	createPool(t, ns, 1000, 1<<30)
	startManager(t, false)

	createRequest(t, ns, "dup", 400, 256<<20, 600)
	waitPhase(t, ns, "dup", quotav1alpha1.PhaseReserved, 30*time.Second)
	if got := getPool(t, ns).Status.UsedCPUMilli; got != 400 {
		t.Fatalf("used = %dm, want 400m", got)
	}

	// Force the status back to Pending as if the status write had been lost.
	rr := getRequest(t, ns, "dup")
	rr.Status.Phase = quotav1alpha1.PhasePending
	rr.Status.ReservedAt = nil
	rr.Status.ExpiresAt = nil
	if err := k8sClient.Status().Update(context.Background(), rr); err != nil {
		t.Fatal(err)
	}

	waitPhase(t, ns, "dup", quotav1alpha1.PhaseReserved, 30*time.Second)
	pool := getPool(t, ns)
	if pool.Status.UsedCPUMilli != 400 || len(pool.Status.Allocations) != 1 {
		t.Fatalf("re-reserve double counted: %+v", pool.Status)
	}
	checkInvariant(t, ns)
}

// TestControllerRestart stops the manager mid-flight and starts a fresh one
// against the same apiserver state: reservations must survive the restart,
// a request whose status write was "lost" must converge without
// double-counting, and new requests must keep reserving correctly.
func TestControllerRestart(t *testing.T) {
	ns := newNamespace(t, false)
	createPool(t, ns, 1000, 1<<30)

	stop := startManager(t, false)
	createRequest(t, ns, "before-restart", 400, 256<<20, 600)
	waitPhase(t, ns, "before-restart", quotav1alpha1.PhaseReserved, 30*time.Second)

	// Simulate a crash right after the ledger update but before the status
	// update for a second request: write the ledger entry's counterpart by
	// forcing the status back to Pending, then kill the manager.
	createRequest(t, ns, "lost-status", 200, 128<<20, 600)
	waitPhase(t, ns, "lost-status", quotav1alpha1.PhaseReserved, 30*time.Second)
	lost := getRequest(t, ns, "lost-status")
	lost.Status.Phase = quotav1alpha1.PhasePending
	lost.Status.ReservedAt = nil
	lost.Status.ExpiresAt = nil
	if err := k8sClient.Status().Update(context.Background(), lost); err != nil {
		t.Fatal(err)
	}
	stop() // controller is fully stopped now

	// Restart a fresh manager against the same state.
	startManager(t, false)
	waitPhase(t, ns, "lost-status", quotav1alpha1.PhaseReserved, 30*time.Second)
	if got := phaseOf(t, ns, "before-restart"); got != quotav1alpha1.PhaseReserved {
		t.Fatalf("before-restart phase = %s after restart, want Reserved", got)
	}
	pool := getPool(t, ns)
	if pool.Status.UsedCPUMilli != 600 || len(pool.Status.Allocations) != 2 {
		t.Fatalf("pool after restart = %+v, want 600m across 2 allocations", pool.Status)
	}

	// New requests still reserve correctly after the restart.
	createRequest(t, ns, "after-restart", 100, 64<<20, 600)
	waitPhase(t, ns, "after-restart", quotav1alpha1.PhaseReserved, 30*time.Second)
	if got := getPool(t, ns).Status.UsedCPUMilli; got != 700 {
		t.Fatalf("used = %dm, want 700m", got)
	}
	checkInvariant(t, ns)
}
