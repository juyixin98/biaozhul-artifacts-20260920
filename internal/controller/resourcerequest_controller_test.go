package controller

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
)

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := quotav1alpha1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

// flakyClient fails the next n status updates with Conflict errors, to prove
// the reconciler's optimistic-concurrency retry loops work.
type flakyClient struct {
	client.Client
	failures *atomic.Int32
}

func (f *flakyClient) Status() client.SubResourceWriter {
	return &flakyStatusWriter{inner: f.Client.Status(), failures: f.failures}
}

type flakyStatusWriter struct {
	inner    client.SubResourceWriter
	failures *atomic.Int32
}

func (w *flakyStatusWriter) Create(ctx context.Context, obj client.Object, sub client.Object, opts ...client.SubResourceCreateOption) error {
	return w.inner.Create(ctx, obj, sub, opts...)
}

func (w *flakyStatusWriter) Update(ctx context.Context, obj client.Object, opts ...client.SubResourceUpdateOption) error {
	if w.failures.Add(-1) >= 0 {
		return apierrors.NewConflict(
			schema.GroupResource{Group: "quota.biaozhu.dev", Resource: "quotapools"},
			obj.GetName(), errors.New("injected conflict"))
	}
	return w.inner.Update(ctx, obj, opts...)
}

func (w *flakyStatusWriter) Patch(ctx context.Context, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
	return w.inner.Patch(ctx, obj, patch, opts...)
}

const ns = "ns"

func newPool(cpu, mem int64) *quotav1alpha1.QuotaPool {
	return &quotav1alpha1.QuotaPool{
		ObjectMeta: metav1.ObjectMeta{Name: "default", Namespace: ns},
		Spec:       quotav1alpha1.QuotaPoolSpec{CPUMilli: cpu, MemoryBytes: mem},
	}
}

func newRequest(name string, cpu, mem, ttl int64) *quotav1alpha1.ResourceRequest {
	return &quotav1alpha1.ResourceRequest{
		ObjectMeta: metav1.ObjectMeta{
			Name:       name,
			Namespace:  ns,
			UID:        types.UID("uid-" + name),
			Finalizers: []string{quotav1alpha1.Finalizer},
		},
		Spec: quotav1alpha1.ResourceRequestSpec{
			Pool: "default", CPUMilli: cpu, MemoryBytes: mem, TTLSeconds: ttl,
		},
	}
}

func newReconciler(t *testing.T, c client.Client) *ResourceRequestReconciler {
	t.Helper()
	return &ResourceRequestReconciler{
		Client:   c,
		Scheme:   testScheme(t),
		Recorder: record.NewFakeRecorder(100),
	}
}

func reconcileOnce(t *testing.T, r *ResourceRequestReconciler, name string) {
	t.Helper()
	_, err := r.Reconcile(context.Background(), reconcile.Request{
		NamespacedName: types.NamespacedName{Namespace: ns, Name: name},
	})
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
}

func getRequest(t *testing.T, c client.Client, name string) quotav1alpha1.ResourceRequest {
	t.Helper()
	var rr quotav1alpha1.ResourceRequest
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, &rr); err != nil {
		t.Fatal(err)
	}
	return rr
}

func getPool(t *testing.T, c client.Client) quotav1alpha1.QuotaPool {
	t.Helper()
	var pool quotav1alpha1.QuotaPool
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: "default"}, &pool); err != nil {
		t.Fatal(err)
	}
	return pool
}

func buildClient(t *testing.T, objs ...client.Object) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithObjects(objs...).
		WithStatusSubresource(&quotav1alpha1.ResourceRequest{}, &quotav1alpha1.QuotaPool{}).
		Build()
}

func TestPendingReservesAndIsIdempotent(t *testing.T) {
	c := buildClient(t, newPool(1000, 1<<30), newRequest("a", 400, 1<<28, 60))
	r := newReconciler(t, c)

	reconcileOnce(t, r, "a")
	rr := getRequest(t, c, "a")
	if rr.Status.Phase != quotav1alpha1.PhaseReserved {
		t.Fatalf("phase = %s, want Reserved", rr.Status.Phase)
	}
	if rr.Status.ExpiresAt == nil || rr.Status.ReservedAt == nil {
		t.Fatal("ReservedAt/ExpiresAt must be set")
	}
	pool := getPool(t, c)
	if pool.Status.UsedCPUMilli != 400 || pool.Status.UsedMemoryBytes != 1<<28 {
		t.Fatalf("pool used = %dm/%dB", pool.Status.UsedCPUMilli, pool.Status.UsedMemoryBytes)
	}

	// Duplicate reconcile of the same request must not double-count.
	reconcileOnce(t, r, "a")
	reconcileOnce(t, r, "a")
	pool = getPool(t, c)
	if pool.Status.UsedCPUMilli != 400 || len(pool.Status.Allocations) != 1 {
		t.Fatalf("double count: used=%dm allocs=%+v", pool.Status.UsedCPUMilli, pool.Status.Allocations)
	}

	// Simulate a crash between the pool update and the status update: the
	// ledger holds the allocation but the request says Pending again.
	rr.Status.Phase = quotav1alpha1.PhasePending
	rr.Status.ReservedAt = nil
	rr.Status.ExpiresAt = nil
	if err := c.Status().Update(context.Background(), &rr); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, "a")
	rr = getRequest(t, c, "a")
	if rr.Status.Phase != quotav1alpha1.PhaseReserved {
		t.Fatalf("phase after re-reserve = %s", rr.Status.Phase)
	}
	pool = getPool(t, c)
	if pool.Status.UsedCPUMilli != 400 || len(pool.Status.Allocations) != 1 {
		t.Fatalf("re-reserve double counted: used=%dm allocs=%+v",
			pool.Status.UsedCPUMilli, pool.Status.Allocations)
	}
}

func TestPendingRejectedWhenCapacityExceeded(t *testing.T) {
	c := buildClient(t, newPool(300, 1<<28), newRequest("big", 400, 1<<28, 60))
	r := newReconciler(t, c)

	reconcileOnce(t, r, "big")
	rr := getRequest(t, c, "big")
	if rr.Status.Phase != quotav1alpha1.PhaseRejected {
		t.Fatalf("phase = %s, want Rejected", rr.Status.Phase)
	}
	if rr.Status.Reason != "InsufficientCapacity" {
		t.Fatalf("reason = %s", rr.Status.Reason)
	}
	pool := getPool(t, c)
	if pool.Status.UsedCPUMilli != 0 || len(pool.Status.Allocations) != 0 {
		t.Fatalf("rejected request must not allocate: %+v", pool.Status)
	}
}

func TestReserveRetriesOnResourceVersionConflict(t *testing.T) {
	base := buildClient(t, newPool(1000, 1<<30), newRequest("a", 400, 1<<28, 60))
	failures := &atomic.Int32{}
	failures.Store(3) // first three status updates fail with Conflict
	c := &flakyClient{Client: base, failures: failures}
	r := newReconciler(t, c)

	reconcileOnce(t, r, "a")
	rr := getRequest(t, base, "a")
	if rr.Status.Phase != quotav1alpha1.PhaseReserved {
		t.Fatalf("phase = %s, want Reserved (conflict retries must succeed)", rr.Status.Phase)
	}
	pool := getPool(t, base)
	if pool.Status.UsedCPUMilli != 400 || len(pool.Status.Allocations) != 1 {
		t.Fatalf("pool after conflict retries: %+v", pool.Status)
	}
}

func TestReservedExpiresAfterTTL(t *testing.T) {
	rr := newRequest("a", 400, 1<<28, 10)
	past := metav1.NewTime(time.Now().Add(-time.Second))
	rr.Status.Phase = quotav1alpha1.PhaseReserved
	rr.Status.ExpiresAt = &past
	pool := newPool(1000, 1<<30)
	pool.Status.Allocations = []quotav1alpha1.Allocation{
		{RequestUID: "uid-a", RequestName: "a", CPUMilli: 400, MemoryBytes: 1 << 28},
	}
	pool.Status.UsedCPUMilli = 400
	pool.Status.UsedMemoryBytes = 1 << 28

	c := buildClient(t, pool, rr)
	r := newReconciler(t, c)
	reconcileOnce(t, r, "a")

	gotRR := getRequest(t, c, "a")
	if gotRR.Status.Phase != quotav1alpha1.PhaseExpired {
		t.Fatalf("phase = %s, want Expired", gotRR.Status.Phase)
	}
	gotPool := getPool(t, c)
	if gotPool.Status.UsedCPUMilli != 0 || len(gotPool.Status.Allocations) != 0 {
		t.Fatalf("expiry must release quota: %+v", gotPool.Status)
	}
}

func TestExpiredSweepsLatePods(t *testing.T) {
	rr := newRequest("a", 400, 1<<28, 10)
	rr.Status.Phase = quotav1alpha1.PhaseExpired
	latePod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "late",
			Namespace: ns,
			Labels:    map[string]string{quotav1alpha1.LabelRequest: "a"},
		},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "c", Image: "pause"}}},
	}
	c := buildClient(t, newPool(1000, 1<<30), rr, latePod)
	r := newReconciler(t, c)
	reconcileOnce(t, r, "a")

	var pod corev1.Pod
	err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: "late"}, &pod)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("late pod must be deleted, get err = %v", err)
	}
}

func TestBoundReleasesWhenPodTerminates(t *testing.T) {
	mkBound := func() (*quotav1alpha1.ResourceRequest, *quotav1alpha1.QuotaPool) {
		rr := newRequest("a", 400, 1<<28, 60)
		rr.Status.Phase = quotav1alpha1.PhaseBound
		rr.Status.BoundPod = "p"
		pool := newPool(1000, 1<<30)
		pool.Status.Allocations = []quotav1alpha1.Allocation{
			{RequestUID: "uid-a", RequestName: "a", CPUMilli: 400, MemoryBytes: 1 << 28},
		}
		pool.Status.UsedCPUMilli = 400
		pool.Status.UsedMemoryBytes = 1 << 28
		return rr, pool
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: ns},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "c", Image: "pause"}}},
	}

	// Pod still running: nothing changes.
	rr, pool := mkBound()
	runningPod := pod.DeepCopy()
	runningPod.Status.Phase = corev1.PodRunning
	c := buildClient(t, pool, rr, runningPod)
	r := newReconciler(t, c)
	reconcileOnce(t, r, "a")
	if got := getRequest(t, c, "a"); got.Status.Phase != quotav1alpha1.PhaseBound {
		t.Fatalf("phase = %s, want Bound while pod runs", got.Status.Phase)
	}
	if got := getPool(t, c); got.Status.UsedCPUMilli != 400 {
		t.Fatalf("quota released too early: %+v", got.Status)
	}

	// Pod succeeded: release.
	rr, pool = mkBound()
	donePod := pod.DeepCopy()
	donePod.Status.Phase = corev1.PodSucceeded
	c = buildClient(t, pool, rr, donePod)
	r = newReconciler(t, c)
	reconcileOnce(t, r, "a")
	if got := getRequest(t, c, "a"); got.Status.Phase != quotav1alpha1.PhaseReleased {
		t.Fatalf("phase = %s, want Released", got.Status.Phase)
	}
	if gotPool := getPool(t, c); gotPool.Status.UsedCPUMilli != 0 || len(gotPool.Status.Allocations) != 0 {
		t.Fatalf("quota not released: %+v", gotPool.Status)
	}

	// Pod deleted: release.
	rr, pool = mkBound()
	c = buildClient(t, pool, rr) // no pod object
	r = newReconciler(t, c)
	reconcileOnce(t, r, "a")
	if got := getRequest(t, c, "a"); got.Status.Phase != quotav1alpha1.PhaseReleased {
		t.Fatalf("phase = %s, want Released after pod deletion", got.Status.Phase)
	}
}

func TestFinalizerReleasesQuotaOnDeletion(t *testing.T) {
	rr := newRequest("a", 400, 1<<28, 60)
	rr.Status.Phase = quotav1alpha1.PhaseReserved
	pool := newPool(1000, 1<<30)
	pool.Status.Allocations = []quotav1alpha1.Allocation{
		{RequestUID: "uid-a", RequestName: "a", CPUMilli: 400, MemoryBytes: 1 << 28},
	}
	pool.Status.UsedCPUMilli = 400
	pool.Status.UsedMemoryBytes = 1 << 28

	c := buildClient(t, pool, rr)
	if err := c.Delete(context.Background(), rr); err != nil {
		t.Fatal(err)
	}
	r := newReconciler(t, c)
	reconcileOnce(t, r, "a")

	if got := getPool(t, c); got.Status.UsedCPUMilli != 0 || len(got.Status.Allocations) != 0 {
		t.Fatalf("finalizer did not release quota: %+v", got.Status)
	}
	var gone quotav1alpha1.ResourceRequest
	err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: "a"}, &gone)
	if !apierrors.IsNotFound(err) {
		t.Fatalf("request should be gone after finalizer removal, get err = %v", err)
	}
}
