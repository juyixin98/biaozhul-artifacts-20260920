package integration

import (
	"context"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	quota "resourcequota-reservation/api/v1alpha1"
)

// TestClaimWebhookValidatesUnitsAndBounds: the admission webhook rejects bad
// units and out-of-range requests before the controller ever sees them — no
// in-memory counting involved.
func TestClaimWebhookValidatesUnitsAndBounds(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()
	ns := newNamespace(t, "ns-wh-claim")
	createPool(t, ns, "4000m", "4Gi")

	bad := []struct {
		name string
		cpu  string
		mem  string
		ttl  string
	}{
		{"bad-cpu-unit", "500x", "128Mi", "10s"},
		{"bad-mem-unit", "500m", "128Xi", "10s"},
		{"cpu-too-big", "70000m", "128Mi", "10s"},
		{"mem-too-big", "1m", "300Gi", "10s"},
		{"zero-ttl", "500m", "128Mi", "0s"},
	}
	for _, b := range bad {
		c := &quota.ResourceClaim{}
		c.Namespace = ns
		c.Name = b.name
		c.Spec = quota.ResourceClaimSpec{CPU: b.cpu, Memory: b.mem, TTL: parseDur(t, b.ttl)}
		err := directClient.Create(context.Background(), c)
		if err == nil {
			_ = directClient.Delete(context.Background(), c)
			t.Errorf("case %s: expected webhook denial", b.name)
			continue
		}
		if !isForbidden(err) {
			t.Errorf("case %s: expected admission error, got %v", b.name, err)
		}
	}

	// A conformant claim is admitted.
	good := createClaim(t, ns, "good", "500m", "128Mi", "30s")
	if good.UID == "" {
		t.Fatal("valid claim was not created")
	}
}

// TestClaimSpecImmutable: changing spec after creation is denied.
func TestClaimSpecImmutable(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()
	ns := newNamespace(t, "ns-wh-immutable")
	createPool(t, ns, "4000m", "4Gi")
	c := createClaim(t, ns, "imm", "500m", "128Mi", "30s")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, c.Name).Status.Phase == quota.PhaseReserved
	}, "reserved")

	got := getClaim(t, ns, c.Name)
	got.Spec.CPU = "600m"
	err := directClient.Update(context.Background(), got)
	if err == nil {
		t.Fatal("spec mutation must be denied by webhook")
	}
	if !isForbidden(err) || !strings.Contains(err.Error(), "immutable") {
		t.Fatalf("expected immutability denial, got %v", err)
	}

	// Metadata-only changes still work.
	got2 := getClaim(t, ns, c.Name)
	got2.Labels = map[string]string{"a": "b"}
	if err := directClient.Update(context.Background(), got2); err != nil {
		t.Fatalf("metadata update should be allowed: %v", err)
	}
}

// TestPodWebhookGuardsReservation: pod admission against Pending/Bound/Expired
// claims and oversized requests.
func TestPodWebhookGuardsReservation(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()
	ns := newNamespace(t, "ns-wh-pod")
	createPool(t, ns, "4000m", "4Gi")

	// 1) pod against a non-existent claim -> denied
	if err := createPod(t, podForClaim(ns, "p-missing", "nope", "100m", "64Mi")); err == nil {
		cleanupPod(t, "p-missing", ns)
		t.Fatal("pod for missing claim must be denied")
	}

	// 2) pod against Pending claim -> denied (race before reservation)
	c := &quota.ResourceClaim{}
	c.Namespace = ns
	c.Name = "p1"
	c.Spec = quota.ResourceClaimSpec{CPU: "500m", Memory: "128Mi", TTL: metav1.Duration{Duration: 10 * time.Minute}}
	if err := directClient.Create(context.Background(), c); err != nil {
		t.Fatal(err)
	}
	if err := createPod(t, podForClaim(ns, "p-early", "p1", "100m", "64Mi")); err == nil {
		cleanupPod(t, "p-early", ns)
		t.Fatal("pod against Pending claim must be denied")
	}

	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "p1").Status.Phase == quota.PhaseReserved
	}, "p1 reserved")

	// 3) oversized pod -> denied
	if err := createPod(t, podForClaim(ns, "p-big", "p1", "600m", "64Mi")); err == nil {
		cleanupPod(t, "p-big", ns)
		t.Fatal("pod exceeding reserved cpu must be denied")
	}

	// 4) correctly-sized pod -> admitted and binds
	pod := podForClaim(ns, "p-ok", "p1", "400m", "100Mi")
	if err := createPod(t, pod); err != nil {
		t.Fatalf("valid pod denied: %v", err)
	}
	defer cleanupPod(t, pod.Name, ns)
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "p1").Status.Phase == quota.PhaseBound
	}, "p1 bound")

	// 5) second pod against the same Bound claim -> denied
	extra := podForClaim(ns, "p-second", "p1", "100m", "32Mi")
	extra.Name = "p-second"
	if err := createPod(t, extra); err == nil {
		cleanupPod(t, extra.Name, ns)
		t.Fatal("second pod against Bound claim must be denied")
	}

	// 6) unlabeled pods are completely unaffected
	plain := podForClaim(ns, "p-plain", "", "1m", "4Mi")
	plain.Labels = nil
	if err := createPod(t, plain); err != nil {
		t.Fatalf("unlabeled pod should be admitted untouched: %v", err)
	}
	defer cleanupPod(t, plain.Name, ns)
}

// TestExpiredThenClaimReuse: after expiry the released capacity is available
// to a new claim, and the expired one stays denied for pods.
func TestExpiredThenClaimReuse(t *testing.T) {
	mgr := startManager(t)
	defer mgr.stop()
	ns := newNamespace(t, "ns-reuse")
	createPool(t, ns, "500m", "512Mi")
	first := createClaim(t, ns, "old", "500m", "512Mi", "2s")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "old").Status.Phase == quota.PhaseReserved
	}, "old reserved")
	eventually(t, 15*time.Second, func() bool {
		return getClaim(t, ns, "old").Status.Phase == quota.PhaseExpired
	}, "old expired")

	next := createClaim(t, ns, "new", "500m", "512Mi", "10m")
	eventually(t, convergeTimeout, func() bool {
		return getClaim(t, ns, "new").Status.Phase == quota.PhaseReserved
	}, "new claim should take the released capacity")

	if pool := getPool(t, ns); pool.Status.ReservedCPU != "500m" {
		t.Fatalf("pool cpu = %s, want 500m reused", pool.Status.ReservedCPU)
	}

	// Late pod against the expired claim still denied.
	if err := createPod(t, podForClaim(ns, "old-late", "old", "100m", "32Mi")); err == nil {
		cleanupPod(t, "old-late", ns)
		t.Fatal("late pod on expired claim must be denied")
	}
	_ = first
	_ = next
}
