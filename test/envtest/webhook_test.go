package envtest

import (
	"context"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/types"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
)

func podForRequest(ns, name, requestName string, cpuMilli, memBytes int64) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: ns,
			Labels:    map[string]string{quotav1alpha1.LabelRequest: requestName},
		},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{{
				Name:  "pause",
				Image: "registry.k8s.io/pause:3.9",
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    *resource.NewMilliQuantity(cpuMilli, resource.DecimalSI),
						corev1.ResourceMemory: *resource.NewQuantity(memBytes, resource.BinarySI),
					},
				},
			}},
		},
	}
}

// TestValidationWebhook exercises the ResourceRequest validating webhook
// against the real apiserver: bounds and spec immutability.
func TestValidationWebhook(t *testing.T) {
	ns := newNamespace(t, true)
	createPool(t, ns, 100000, 100<<30)
	startManager(t, true)

	// Out-of-range values are rejected (CRD schema + webhook bounds agree).
	bad := &quotav1alpha1.ResourceRequest{
		ObjectMeta: metav1.ObjectMeta{Name: "bad-ttl", Namespace: ns},
		Spec: quotav1alpha1.ResourceRequestSpec{
			Pool: "default", CPUMilli: 100, MemoryBytes: 1 << 20, TTLSeconds: 5,
		},
	}
	if err := k8sClient.Create(context.Background(), bad); err == nil {
		t.Fatal("ttlSeconds=5 must be rejected")
	}

	good := createRequest(t, ns, "good", 100, 1<<20, 60)

	// Spec is immutable after creation (webhook-only rule).
	good.Spec.CPUMilli = 200
	err := k8sClient.Update(context.Background(), good)
	if err == nil || !strings.Contains(err.Error(), "immutable") {
		t.Fatalf("spec update must be forbidden as immutable, got %v", err)
	}
}

// TestBindingLifecycle walks the happy path with real objects:
// reserve -> admit pod (webhook binds) -> Bound -> delete pod -> Released,
// checking the pool ledger stays consistent with the real pod throughout.
func TestBindingLifecycle(t *testing.T) {
	ns := newNamespace(t, true)
	createPool(t, ns, 1000, 1<<30)
	startManager(t, true)

	createRequest(t, ns, "bind", 500, 256<<20, 300)
	waitPhase(t, ns, "bind", quotav1alpha1.PhaseReserved, 30*time.Second)

	// A pod whose resources do not match the reservation is rejected.
	badPod := podForRequest(ns, "bad-pod", "bind", 400, 256<<20)
	if err := k8sClient.Create(context.Background(), badPod); err == nil ||
		!strings.Contains(err.Error(), "do not match") {
		t.Fatalf("mismatched pod must be rejected, got %v", err)
	}

	// A matching pod is admitted and atomically binds the reservation.
	pod := podForRequest(ns, "real-pod", "bind", 500, 256<<20)
	if err := k8sClient.Create(context.Background(), pod); err != nil {
		t.Fatalf("matching pod must be admitted: %v", err)
	}
	waitPhase(t, ns, "bind", quotav1alpha1.PhaseBound, 15*time.Second)
	rr := getRequest(t, ns, "bind")
	if rr.Status.BoundPod != "real-pod" {
		t.Fatalf("boundPod = %q, want real-pod", rr.Status.BoundPod)
	}
	pool := getPool(t, ns)
	if pool.Status.UsedCPUMilli != 500 || len(pool.Status.Allocations) != 1 {
		t.Fatalf("pool while bound = %+v", pool.Status)
	}
	if pool.Status.Allocations[0].RequestUID != rr.UID {
		t.Fatalf("ledger UID %s != request UID %s", pool.Status.Allocations[0].RequestUID, rr.UID)
	}

	// A second pod for the same request is rejected: already bound.
	second := podForRequest(ns, "second-pod", "bind", 500, 256<<20)
	if err := k8sClient.Create(context.Background(), second); err == nil ||
		!strings.Contains(err.Error(), "already bound") {
		t.Fatalf("second pod must be rejected as already bound, got %v", err)
	}

	// Deleting the pod releases the reservation.
	if err := k8sClient.Delete(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	waitPhase(t, ns, "bind", quotav1alpha1.PhaseReleased, 30*time.Second)
	if got := getPool(t, ns).Status.UsedCPUMilli; got != 0 {
		t.Fatalf("used = %dm after pod deletion, want 0", got)
	}
	checkInvariant(t, ns)
}

// TestExpiryAndLatePod proves the explicit Reserved -> Expired transition and
// that a pod arriving after expiry is rejected by the admission webhook.
func TestExpiryAndLatePod(t *testing.T) {
	ns := newNamespace(t, true)
	createPool(t, ns, 1000, 1<<30)
	startManager(t, true)

	createRequest(t, ns, "short-lived", 300, 128<<20, 10)
	waitPhase(t, ns, "short-lived", quotav1alpha1.PhaseReserved, 30*time.Second)
	if got := getPool(t, ns).Status.UsedCPUMilli; got != 300 {
		t.Fatalf("used = %dm, want 300m", got)
	}

	// TTL elapses: explicit transition to Expired, quota released.
	waitPhase(t, ns, "short-lived", quotav1alpha1.PhaseExpired, 60*time.Second)
	if got := getPool(t, ns).Status.UsedCPUMilli; got != 0 {
		t.Fatalf("used = %dm after expiry, want 0", got)
	}

	// The late pod is rejected at admission with an expiry reason.
	latePod := podForRequest(ns, "late-pod", "short-lived", 300, 128<<20)
	err := k8sClient.Create(context.Background(), latePod)
	if err == nil {
		_ = k8sClient.Delete(context.Background(), latePod)
		t.Fatal("late pod must be rejected after expiry")
	}
	if !strings.Contains(strings.ToLower(err.Error()), "expired") {
		t.Fatalf("late pod rejection must mention expiry, got %v", err)
	}

	// Pods without a reservation label are rejected in managed namespaces.
	unlabeled := podForRequest(ns, "unlabeled", "", 10, 16<<20)
	delete(unlabeled.Labels, quotav1alpha1.LabelRequest)
	if err := k8sClient.Create(context.Background(), unlabeled); err == nil ||
		!strings.Contains(err.Error(), "must carry label") {
		t.Fatalf("unlabeled pod must be rejected, got %v", err)
	}
	checkInvariant(t, ns)
}

// TestPendingPodRejected covers a pod arriving before its reservation is
// ready: the request is still Pending (its pool does not exist), so admission
// must fail closed.
func TestPendingPodRejected(t *testing.T) {
	ns := newNamespace(t, true)
	// Note: no pool — the request can never leave Pending.
	startManager(t, true)

	rr := &quotav1alpha1.ResourceRequest{
		ObjectMeta: metav1.ObjectMeta{Name: "waiting", Namespace: ns},
		Spec: quotav1alpha1.ResourceRequestSpec{
			Pool: "default", CPUMilli: 100, MemoryBytes: 64 << 20, TTLSeconds: 60,
		},
	}
	if err := k8sClient.Create(context.Background(), rr); err != nil {
		t.Fatal(err)
	}
	pod := podForRequest(ns, "early-pod", "waiting", 100, 64<<20)
	err := k8sClient.Create(context.Background(), pod)
	if err == nil {
		_ = k8sClient.Delete(context.Background(), pod)
		t.Fatal("pod for a Pending request must be rejected")
	}
	if !apierrors.IsInvalid(err) && !strings.Contains(err.Error(), "Pending") {
		t.Fatalf("rejection should mention the Pending phase, got %v", err)
	}
	// Cleanup: the request stays Pending; nothing reserved.
	var got quotav1alpha1.ResourceRequest
	if err := k8sClient.Get(context.Background(),
		types.NamespacedName{Namespace: ns, Name: "waiting"}, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase == quotav1alpha1.PhaseBound {
		t.Fatal("request must not be bound by a rejected pod")
	}
}
