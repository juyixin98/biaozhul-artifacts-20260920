package integration

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	quota "resourcequota-reservation/api/v1alpha1"
	"resourcequota-reservation/internal/ledger"
)

const convergeTimeout = 20 * time.Second

// TestBasicReservationShowsConsistentTriangle verifies the core invariant:
// ResourceClaim totals == ReservationPool ledger == backing Pod requests.
func TestBasicReservationShowsConsistentTriangle(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()

	ns := newNamespace(t, "ns-basic")
	createPool(t, ns, "2000m", "2Gi")
	c := createClaim(t, ns, "c1", "500m", "512Mi", "2m")

	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "c1").Status.Phase == quota.PhaseReserved
	}, "claim should become Reserved")

	pool := getPool(t, ns)
	if pool.Status.ReservedCPU != "500m" || pool.Status.ReservedMemory != "512Mi" {
		t.Fatalf("pool totals after reserve = %s/%s, want 500m/512Mi",
			pool.Status.ReservedCPU, pool.Status.ReservedMemory)
	}
	if _, ok := pool.Status.Claims[string(c.UID)]; !ok {
		t.Fatal("pool ledger missing claim UID entry")
	}

	// Create the real object: a Pod whose requests fit the reservation.
	pod := podForClaim(ns, "pod-c1", "c1", "400m", "256Mi")
	if err := createPod(t, pod); err != nil {
		t.Fatalf("create pod: %v", err)
	}
	defer cleanupPod(t, pod.Name, ns)

	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "c1").Status.Phase == quota.PhaseBound
	}, "claim should become Bound")
	if got := getClaim(t, ns, "c1").Status.BoundPod; got != "pod-c1" {
		t.Fatalf("BoundPod = %q, want pod-c1", got)
	}

	// Capacity stays reserved while the real Pod exists — the triangle holds:
	// pool totals == sum(Reserved+Bound claims) == pod consumption covered.
	pool = getPool(t, ns)
	if pool.Status.ReservedCPU != "500m" {
		t.Fatalf("pool cpu changed after bind: %s", pool.Status.ReservedCPU)
	}

	// Delete the Pod: claim should move back to Reserved (TTL still valid).
	if err := directClient.Delete(context.Background(), pod); err != nil {
		t.Fatalf("delete pod: %v", err)
	}
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "c1").Status.Phase == quota.PhaseReserved
	}, "claim should return to Reserved after pod deletion")
}

// TestConcurrentApplicationsNeverOvercommit: 10 claims of 300m against a
// 2000m pool created in parallel. Exactly 6 must reserve, 4 must be rejected,
// and the pool must never show more than 2000m / six ledger entries.
func TestConcurrentApplicationsNeverOvercommit(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()

	ns := newNamespace(t, "ns-concurrent")
	createPool(t, ns, "2000m", "10Gi")

	const n = 10
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			c := &quota.ResourceClaim{}
			c.Namespace = ns
			c.Name = fmt.Sprintf("cc-%02d", i)
			c.Spec = quota.ResourceClaimSpec{
				CPU: "300m", Memory: "100Mi",
				TTL: metav1.Duration{Duration: 2 * time.Minute},
			}
			if err := directClient.Create(context.Background(), c); err != nil {
				errs <- err
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("create claim: %v", err)
	}

	var reserved, rejected int
	eventually(t, convergeTimeout, func() bool {
		reserved, rejected = 0, 0
		var list quota.ResourceClaimList
		if err := directClient.List(context.Background(), &list, client.InNamespace(ns)); err != nil {
			return false
		}
		for i := range list.Items {
			switch list.Items[i].Status.Phase {
			case quota.PhaseReserved:
				reserved++
			case quota.PhaseRejected:
				rejected++
			}
		}
		return reserved+rejected == n
	}, "all claims should reach a terminal phase")

	if reserved != 6 {
		t.Errorf("reserved = %d, want 6", reserved)
	}
	if rejected != 4 {
		t.Errorf("rejected = %d, want 4", rejected)
	}
	pool := getPool(t, ns)
	if pool.Status.ReservedCPU != "1800m" {
		t.Errorf("pool ReservedCPU = %s, want 1800m", pool.Status.ReservedCPU)
	}
	if len(pool.Status.Claims) != 6 {
		t.Errorf("pool ledger entries = %d, want 6 (no double debit)", len(pool.Status.Claims))
	}
}

// TestControllerRestartIdempotency: reservations made before a crash remain in
// the pool; after restart new claims work and old ones are never debited twice.
func TestControllerRestartIdempotency(t *testing.T) {
	mgr := startManager(t)
	ns := newNamespace(t, "ns-restart")
	createPool(t, ns, "4000m", "4Gi")

	c1 := createClaim(t, ns, "r1", "400m", "256Mi", "10m")
	c2 := createClaim(t, ns, "r2", "400m", "256Mi", "10m")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "r1").Status.Phase == quota.PhaseReserved &&
			getClaim(t, ns, "r2").Status.Phase == quota.PhaseReserved
	}, "both claims reserved before crash")

	// Crash: stop the manager while objects stay in etcd.
	mgr.stop()
	time.Sleep(2 * time.Second)

	mgr2 := startManager(t)
	defer mgr2.stop()

	// On startup the fresh informer re-lists and re-reconciles every claim.
	// Wait long enough for that to happen, then create a new claim — totals
	// must show exactly one debit per claim, never two.
	time.Sleep(3 * time.Second)
	c3 := createClaim(t, ns, "r3", "200m", "128Mi", "10m")

	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "r3").Status.Phase == quota.PhaseReserved
	}, "new claim should reserve after restart")

	pool := getPool(t, ns)
	// Canonical rendering of 1000m is "1"; compare integer milli values.
	cpu, err := ledger.ParseCPU(pool.Status.ReservedCPU)
	if err != nil {
		t.Fatal(err)
	}
	if cpu != 1000 {
		t.Errorf("cpu after restart = %dm (raw %q), want 1000m", cpu, pool.Status.ReservedCPU)
	}
	if pool.Status.ReservedMemory != "640Mi" {
		t.Errorf("mem after restart = %s, want 640Mi", pool.Status.ReservedMemory)
	}
	if len(pool.Status.Claims) != 3 {
		t.Errorf("ledger entries = %d, want 3 — pre-crash claims must not be debited twice",
			len(pool.Status.Claims))
	}
	for _, c := range []*quota.ResourceClaim{c1, c2, c3} {
		if _, ok := pool.Status.Claims[string(c.UID)]; !ok {
			t.Errorf("ledger missing UID for %s", c.Name)
		}
	}
}

// TestResourceVersionConflictUnderContention: a noisy actor mutates the pool
// while claims reserve. Optimistic retries must converge with exact totals.
func TestResourceVersionConflictUnderContention(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()

	ns := newNamespace(t, "ns-conflict")
	createPool(t, ns, "8000m", "8Gi")

	stopChaos := make(chan struct{})
	var chaos sync.WaitGroup
	chaos.Add(1)
	go func() {
		defer chaos.Done()
		i := 0
		for {
			select {
			case <-stopChaos:
				return
			default:
			}
			var p quota.ReservationPool
			if err := directClient.Get(context.Background(),
				types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &p); err != nil {
				time.Sleep(5 * time.Millisecond)
				continue
			}
			i++
			p.Labels = map[string]string{"chaos": fmt.Sprintf("%d", i)}
			_ = directClient.Update(context.Background(), &p) // metadata update bumps RV
			time.Sleep(5 * time.Millisecond)
		}
	}()

	const n = 5
	for i := 0; i < n; i++ {
		createClaim(t, ns, fmt.Sprintf("k%02d", i), "500m", "256Mi", "10m")
	}
	eventually(t, 30*time.Second, func() bool {
		var list quota.ResourceClaimList
		_ = directClient.List(context.Background(), &list, client.InNamespace(ns))
		reserved := 0
		for i := range list.Items {
			if list.Items[i].Status.Phase == quota.PhaseReserved {
				reserved++
			}
		}
		return reserved == n
	}, "all claims should reserve despite pool resourceVersion conflicts")

	close(stopChaos)
	chaos.Wait()

	pool := getPool(t, ns)
	if pool.Status.ReservedCPU != "2500m" {
		t.Errorf("cpu = %s, want 2500m", pool.Status.ReservedCPU)
	}
	if len(pool.Status.Claims) != n {
		t.Errorf("ledger entries = %d, want %d", len(pool.Status.Claims), n)
	}
}

// TestExpiryReleasesCapacityAndRejectsLatePod: after TTL the claim becomes
// Expired, capacity returns to the pool, and the webhook refuses a late Pod.
func TestExpiryReleasesCapacityAndRejectsLatePod(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()

	ns := newNamespace(t, "ns-expiry")
	createPool(t, ns, "1000m", "1Gi")
	createClaim(t, ns, "e1", "400m", "256Mi", "2s")

	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "e1").Status.Phase == quota.PhaseReserved
	}, "claim reserved")
	pool := getPool(t, ns)
	if pool.Status.ReservedCPU != "400m" {
		t.Fatalf("cpu before expiry = %s", pool.Status.ReservedCPU)
	}

	eventually(t, 15*time.Second, func() bool {
		return getClaim(t, ns, "e1").Status.Phase == quota.PhaseExpired
	}, "claim should expire after ttl")

	pool = getPool(t, ns)
	if pool.Status.ReservedCPU != "0" || pool.Status.ReservedMemory != "0" {
		t.Fatalf("capacity not released: %s/%s", pool.Status.ReservedCPU, pool.Status.ReservedMemory)
	}
	if len(pool.Status.Claims) != 0 {
		t.Fatalf("ledger still has %d entries after expiry", len(pool.Status.Claims))
	}

	// Late Pod must be denied by the validating webhook.
	pod := podForClaim(ns, "late-pod", "e1", "100m", "64Mi")
	err := createPod(t, pod)
	if err == nil {
		cleanupPod(t, pod.Name, ns)
		t.Fatal("late pod creation must be rejected by the webhook")
	}
	if !isForbidden(err) {
		t.Fatalf("expected admission forbidden/invalid error, got: %v", err)
	}
}

// TestExpiryVsRealObjectStateTransition: a Pod that exists when the TTL fires
// keeps the reservation (real object wins). Additionally, if a Pod appears in
// the narrow release window (simulated here by removing the admission webhook,
// which stands in for a Pod admitted exactly at the expiry boundary), the
// controller must roll forward Expired -> Bound and re-debit capacity instead
// of leaving claim, pool and Pod inconsistent.
func TestExpiryVsRealObjectStateTransition(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()

	ns := newNamespace(t, "ns-race")
	createPool(t, ns, "1000m", "1Gi")

	// --- Part A: Pod present before TTL -> stays Bound, capacity retained ---
	c1 := createClaim(t, ns, "g1", "300m", "128Mi", "2s")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "g1").Status.Phase == quota.PhaseReserved
	}, "g1 reserved")

	pod1 := podForClaim(ns, "pod-g1", "g1", "200m", "64Mi")
	if err := createPod(t, pod1); err != nil {
		t.Fatalf("create pod-g1: %v", err)
	}
	defer cleanupPod(t, pod1.Name, ns)
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "g1").Status.Phase == quota.PhaseBound
	}, "g1 bound")

	// Wait well beyond the 2s TTL: it must NOT expire while the Pod exists.
	time.Sleep(4 * time.Second)
	if got := getClaim(t, ns, "g1").Status.Phase; got != quota.PhaseBound {
		t.Fatalf("g1 phase after ttl = %s, want Bound (real object wins)", got)
	}
	if pool := getPool(t, ns); pool.Status.ReservedCPU != "300m" {
		t.Fatalf("pool cpu = %s, want 300m retained", pool.Status.ReservedCPU)
	}

	// --- Part B: Pod appears in the release window after Expired ------------
	c2 := createClaim(t, ns, "g2", "200m", "64Mi", "2s")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "g2").Status.Phase == quota.PhaseReserved
	}, "g2 reserved")
	eventually(t, 15*time.Second, func() bool {
		return getClaim(t, ns, "g2").Status.Phase == quota.PhaseExpired
	}, "g2 expired")
	if pool := getPool(t, ns); pool.Status.ReservedCPU != "300m" {
		t.Fatalf("pool cpu after g2 expiry = %s, want 300m", pool.Status.ReservedCPU)
	}

	// Simulate a Pod that won the race against admission (delete the webhook
	// configuration temporarily, create the Pod, restore it).
	whName := "reservation-pod-validator"
	var whCfg admissionCfg
	key := types.NamespacedName{Name: whName}
	if err := directClient.Get(context.Background(), key, &whCfg.object); err != nil {
		t.Fatalf("get pod webhook config: %v", err)
	}
	whCfg.snapshot = whCfg.object.DeepCopy()
	if err := directClient.Delete(context.Background(), &whCfg.object); err != nil {
		t.Fatalf("delete webhook config: %v", err)
	}
	t.Cleanup(func() {
		snap := whCfg.snapshot
		snap.ResourceVersion = ""
		_ = directClient.Create(context.Background(), snap)
	})

	pod2 := podForClaim(ns, "pod-g2-late", "g2", "100m", "32Mi")
	if err := createPod(t, pod2); err != nil {
		t.Fatalf("create late pod-g2 (webhook bypassed): %v", err)
	}
	defer cleanupPod(t, pod2.Name, ns)

	// Controller must observe the real Pod and roll forward to Bound with a
	// fresh debit — no orphaned Pod, no missing capacity.
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "g2").Status.Phase == quota.PhaseBound
	}, "g2 should recover Expired -> Bound via late-pod rule")
	if bp := getClaim(t, ns, "g2").Status.BoundPod; bp != "pod-g2-late" {
		t.Fatalf("g2 BoundPod = %q", bp)
	}
	if pool := getPool(t, ns); pool.Status.ReservedCPU != "500m" {
		t.Fatalf("pool cpu after recovery = %s, want 500m (g1+g2)", pool.Status.ReservedCPU)
	}

	// Keep c1 referenced so go vet doesn't complain in future edits.
	_ = c1
	_ = c2
}

// TestDeletionReleasesCapacity: deleting a Reserved claim credits the pool and
// the finalizer is removed (object actually disappears).
func TestDeletionReleasesCapacity(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()

	ns := newNamespace(t, "ns-delete")
	createPool(t, ns, "1000m", "1Gi")
	c := createClaim(t, ns, "d1", "600m", "512Mi", "10m")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "d1").Status.Phase == quota.PhaseReserved
	}, "reserved")

	if err := directClient.Delete(context.Background(), c); err != nil {
		t.Fatalf("delete: %v", err)
	}
	eventually(t, convergeTimeout, func() bool {
		var got quota.ResourceClaim
		err := directClient.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: "d1"}, &got)
		return apierrors.IsNotFound(err)
	}, "claim object should be gone after finalizer")
	if pool := getPool(t, ns); pool.Status.ReservedCPU != "0" {
		t.Fatalf("pool cpu after delete = %s, want 0", pool.Status.ReservedCPU)
	}
}

// TestClaimPoolAndLedgerSumsAgree is a cross-check helper: sum of active
// claims equals pool totals (used in the concurrency/restart tests implicitly;
// asserted explicitly here).
func TestClaimPoolAndLedgerSumsAgree(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()
	ns := newNamespace(t, "ns-sums")
	createPool(t, ns, "4000m", "4Gi")
	for i, cpu := range []string{"500m", "250m", "750m"} {
		createClaim(t, ns, fmt.Sprintf("s%d", i), cpu, "100Mi", "10m")
	}
	eventually(t, convergeTimeout, func() bool {
		pool := getPool(t, ns)
		var list quota.ResourceClaimList
		_ = directClient.List(context.Background(), &list, client.InNamespace(ns))
		return pool.Status.ReservedCPU == "1500m" && len(list.Items) == 3
	}, "expected 1500m reserved by three claims")

	pool := getPool(t, ns)
	var sumCPU int64
	var list quota.ResourceClaimList
	if err := directClient.List(context.Background(), &list, client.InNamespace(ns)); err != nil {
		t.Fatal(err)
	}
	for i := range list.Items {
		m, err := ledger.ParseCPU(list.Items[i].Status.ReservedCPU)
		if err != nil {
			t.Fatal(err)
		}
		sumCPU += m
	}
	poolCPU, err := ledger.ParseCPU(pool.Status.ReservedCPU)
	if err != nil {
		t.Fatal(err)
	}
	if sumCPU != poolCPU {
		t.Fatalf("sum of claim reservedCPU=%dm disagrees with pool=%dm", sumCPU, poolCPU)
	}
}
