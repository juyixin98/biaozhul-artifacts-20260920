package controller

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	quota "resourcequota-reservation/api/v1alpha1"
)

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := quota.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

// --- client wrappers --------------------------------------------------------

// failingStatusClient wraps a client so the first N status updates targeting a
// ReservationPool fail with a resource-version Conflict — exactly what the API
// server returns when two writers race.
type failingStatusClient struct {
	client.Client
	failuresLeft int
}

func (c *failingStatusClient) Status() client.SubResourceWriter {
	return &conflictSubResource{
		SubResourceWriter: c.Client.Status(),
		owner:             c,
	}
}

type conflictSubResource struct {
	client.SubResourceWriter
	owner *failingStatusClient
}

func (s *conflictSubResource) Update(ctx context.Context, obj client.Object, opts ...client.SubResourceUpdateOption) error {
	if _, isPool := obj.(*quota.ReservationPool); isPool && s.owner.failuresLeft > 0 {
		s.owner.failuresLeft--
		return apierrors.NewConflict(
			schema.GroupResource{Group: "quota.example.com", Resource: "reservationpools"},
			obj.GetName(),
			nil,
		)
	}
	return s.SubResourceWriter.Update(ctx, obj, opts...)
}

// --- tests ------------------------------------------------------------------

func newClaim(ns, name string) *quota.ResourceClaim {
	c := &quota.ResourceClaim{}
	c.Namespace = ns
	c.Name = name
	c.UID = types.UID("uid-" + name)
	c.Finalizers = []string{quota.ClaimFinalizer}
	c.Spec = quota.ResourceClaimSpec{
		CPU:    "500m",
		Memory: "512Mi",
		TTL:    metav1.Duration{Duration: time.Minute},
	}
	return c
}

func newPool(ns string) *quota.ReservationPool {
	p := &quota.ReservationPool{}
	p.Namespace = ns
	p.Name = quota.PoolName
	p.Spec = quota.PoolSpec{CPUCapacity: "2000m", MemoryCapacity: "2Gi"}
	return p
}

// TestReconcileRetriesResourceVersionConflict: three simulated conflicts while
// debiting the pool are retried transparently; the claim ends Reserved with
// exact pool totals and a non-zero ConflictCount.
func TestReconcileRetriesResourceVersionConflict(t *testing.T) {
	s := testScheme(t)
	ns := "ns-test"
	c := newClaim(ns, "conflict")
	base := fake.NewClientBuilder().WithScheme(s).
		WithObjects(newPool(ns), c).
		WithStatusSubresource(&quota.ReservationPool{}, &quota.ResourceClaim{}).
		Build()
	cli := &failingStatusClient{Client: base, failuresLeft: 3}

	r := &ClaimReconciler{Client: cli, Scheme: s}
	for i := 0; i < 5; i++ {
		_, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(c)})
		if err != nil {
			t.Fatalf("reconcile %d: %v", i, err)
		}
		var got quota.ResourceClaim
		if err := cli.Get(context.Background(), client.ObjectKeyFromObject(c), &got); err != nil {
			t.Fatal(err)
		}
		if got.Status.Phase == quota.PhaseReserved {
			if got.Status.ConflictCount == 0 {
				t.Fatal("ConflictCount should record the injected conflicts")
			}
			break
		}
	}

	var got quota.ResourceClaim
	if err := cli.Get(context.Background(), client.ObjectKeyFromObject(c), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != quota.PhaseReserved {
		t.Fatalf("phase = %s, want Reserved", got.Status.Phase)
	}
	if got.Status.ConflictCount < 3 {
		t.Fatalf("ConflictCount = %d, want >= 3", got.Status.ConflictCount)
	}

	var pool quota.ReservationPool
	if err := cli.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool); err != nil {
		t.Fatal(err)
	}
	if pool.Status.ReservedCPU != "500m" || pool.Status.ReservedMemory != "512Mi" {
		t.Fatalf("pool totals = %s/%s, want 500m/512Mi", pool.Status.ReservedCPU, pool.Status.ReservedMemory)
	}
}

// TestRepeatedReconcileDoesNotDoubleDebit: reconciling an already-Reserved
// claim repeatedly must leave exactly one ledger entry and unchanged totals —
// this is the restart/requeue idempotency guarantee at the controller level.
func TestRepeatedReconcileDoesNotDoubleDebit(t *testing.T) {
	s := testScheme(t)
	ns := "ns-test"
	c := newClaim(ns, "repeat")
	base := fake.NewClientBuilder().WithScheme(s).
		WithObjects(newPool(ns), c).
		WithStatusSubresource(&quota.ReservationPool{}, &quota.ResourceClaim{}).
		Build()

	r := &ClaimReconciler{Client: base, Scheme: s}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(c)}
	for i := 0; i < 4; i++ {
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatalf("reconcile %d: %v", i, err)
		}
	}

	var pool quota.ReservationPool
	if err := base.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool); err != nil {
		t.Fatal(err)
	}
	if len(pool.Status.Claims) != 1 {
		t.Fatalf("ledger entries = %d, want 1", len(pool.Status.Claims))
	}
	if pool.Status.ReservedCPU != "500m" {
		t.Fatalf("cpu = %s, want 500m", pool.Status.ReservedCPU)
	}
}

// TestReconcileRejectsOverCapacityAndExpiryRefunds covers the rejection path
// and the Reserved -> Expired credit path without a running manager.
func TestReconcileRejectsOverCapacityAndExpiryRefunds(t *testing.T) {
	s := testScheme(t)
	ns := "ns-test"

	// Fill the pool with one big claim, then request more than the remainder.
	big := newClaim(ns, "big")
	big.Spec.CPU = "1800m"
	pending := newClaim(ns, "small")
	pending.UID = "uid-small"
	base := fake.NewClientBuilder().WithScheme(s).
		WithObjects(newPool(ns), big, pending).
		WithStatusSubresource(&quota.ReservationPool{}, &quota.ResourceClaim{}).
		Build()

	fixed := time.Now()
	r := &ClaimReconciler{
		Client: base,
		Scheme: s,
		Now: func() time.Time {
			// Time advances by an hour on every call so expiry deterministically fires.
			fixed = fixed.Add(time.Hour)
			return fixed
		},
	}

	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(big)}); err != nil {
		t.Fatalf("reconcile big: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pending)}); err != nil {
		t.Fatalf("reconcile small: %v", err)
	}

	var small quota.ResourceClaim
	if err := base.Get(context.Background(), client.ObjectKeyFromObject(pending), &small); err != nil {
		t.Fatal(err)
	}
	if small.Status.Phase != quota.PhaseRejected {
		t.Fatalf("small phase = %s, want Rejected", small.Status.Phase)
	}

	// Now let the big claim expire: reconcile at an advanced clock.
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(big)}); err != nil {
		t.Fatalf("reconcile big expiry: %v", err)
	}
	var expired quota.ResourceClaim
	if err := base.Get(context.Background(), client.ObjectKeyFromObject(big), &expired); err != nil {
		t.Fatal(err)
	}
	if expired.Status.Phase != quota.PhaseExpired {
		t.Fatalf("big phase = %s, want Expired", expired.Status.Phase)
	}
	var pool quota.ReservationPool
	if err := base.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool); err != nil {
		t.Fatal(err)
	}
	if pool.Status.ReservedCPU != "0" || len(pool.Status.Claims) != 0 {
		t.Fatalf("pool after expiry: cpu=%s entries=%d, want 0/0",
			pool.Status.ReservedCPU, len(pool.Status.Claims))
	}
}

// TestExpiredClaimWithLivePodRebinds exercises the explicit Expired -> Bound
// state transition when a real Pod is found (late-pod rule).
func TestExpiredClaimWithLivePodRebinds(t *testing.T) {
	s := testScheme(t)
	ns := "ns-test"
	c := newClaim(ns, "late")
	reservedAt := metav1.NewTime(time.Now().Add(-2 * time.Hour))
	expiresAt := metav1.NewTime(time.Now().Add(-time.Hour))
	c.Status = quota.ReservationStatus{
		Phase:      quota.PhaseExpired,
		ExpiresAt:  &expiresAt,
		ReservedAt: &reservedAt,
	}
	pod := &corev1.Pod{}
	pod.Namespace = ns
	pod.Name = "late-pod"
	pod.Labels = map[string]string{quota.ClaimLabelKey: "late"}

	base := fake.NewClientBuilder().WithScheme(s).
		WithObjects(newPool(ns), c, pod).
		WithStatusSubresource(&quota.ReservationPool{}, &quota.ResourceClaim{}).
		Build()
	r := &ClaimReconciler{Client: base, Scheme: s}

	if _, err := r.Reconcile(context.Background(),
		ctrl.Request{NamespacedName: client.ObjectKeyFromObject(c)}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got quota.ResourceClaim
	if err := base.Get(context.Background(), client.ObjectKeyFromObject(c), &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != quota.PhaseBound || got.Status.BoundPod != "late-pod" {
		t.Fatalf("got phase=%s pod=%s, want Bound/late-pod", got.Status.Phase, got.Status.BoundPod)
	}
	var pool quota.ReservationPool
	if err := base.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool); err != nil {
		t.Fatal(err)
	}
	if pool.Status.ReservedCPU != "500m" {
		t.Fatalf("pool cpu after late-pod rebind = %s, want 500m", pool.Status.ReservedCPU)
	}
}
