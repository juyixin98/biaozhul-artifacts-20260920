package webhook

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

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

// flakyClient fails the next n status updates with a Conflict error, to prove
// the webhook's optimistic-concurrency retry loop works.
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
			schema.GroupResource{Group: "quota.biaozhu.dev", Resource: "resourcerequests"},
			obj.GetName(), errors.New("injected conflict"))
	}
	return w.inner.Update(ctx, obj, opts...)
}

func (w *flakyStatusWriter) Patch(ctx context.Context, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
	return w.inner.Patch(ctx, obj, patch, opts...)
}

func reservedRequest(name string, cpu, mem, ttl int64) *quotav1alpha1.ResourceRequest {
	now := metav1.Now()
	exp := metav1.NewTime(now.Add(time.Duration(ttl) * time.Second))
	return &quotav1alpha1.ResourceRequest{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "ns", UID: types.UID("uid-" + name)},
		Spec: quotav1alpha1.ResourceRequestSpec{
			Pool: "default", CPUMilli: cpu, MemoryBytes: mem, TTLSeconds: ttl,
		},
		Status: quotav1alpha1.ResourceRequestStatus{
			Phase:      quotav1alpha1.PhaseReserved,
			ReservedAt: &now,
			ExpiresAt:  &exp,
		},
	}
}

func podFor(name, requestName string, cpuMilli, memBytes int64) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "ns",
			Labels:    map[string]string{quotav1alpha1.LabelRequest: requestName},
		},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{{
				Name: "c",
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

func createRequest(t *testing.T, pod *corev1.Pod) admission.Request {
	t.Helper()
	raw, err := json.Marshal(pod)
	if err != nil {
		t.Fatal(err)
	}
	return admission.Request{AdmissionRequest: admissionv1.AdmissionRequest{
		Operation: admissionv1.Create,
		Namespace: pod.Namespace,
		Name:      pod.Name,
		Object:    runtime.RawExtension{Raw: raw},
	}}
}

func newAdmission(t *testing.T, c client.Client) *PodAdmission {
	t.Helper()
	return &PodAdmission{Client: c, Decoder: admission.NewDecoder(testScheme(t))}
}

func handle(t *testing.T, a *PodAdmission, pod *corev1.Pod) admission.Response {
	t.Helper()
	return a.Handle(context.Background(), createRequest(t, pod))
}

func TestPodAdmissionNoLabel(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	pod := podFor("p", "", 500, 1<<28)
	delete(pod.Labels, quotav1alpha1.LabelRequest)
	resp := handle(t, newAdmission(t, c), pod)
	if resp.Allowed || !strings.Contains(resp.Result.Message, "must carry label") {
		t.Fatalf("want denial about missing label, got %+v", resp.Result)
	}
}

func TestPodAdmissionRequestNotFound(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).Build()
	resp := handle(t, newAdmission(t, c), podFor("p", "missing", 500, 1<<28))
	if resp.Allowed || !strings.Contains(resp.Result.Message, "not found") {
		t.Fatalf("want denial about missing request, got %+v", resp.Result)
	}
}

func TestPodAdmissionWrongPhase(t *testing.T) {
	rr := reservedRequest("r", 500, 1<<28, 60)
	rr.Status.Phase = quotav1alpha1.PhasePending
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	resp := handle(t, newAdmission(t, c), podFor("p", "r", 500, 1<<28))
	if resp.Allowed || !strings.Contains(resp.Result.Message, "Pending") {
		t.Fatalf("want denial mentioning phase, got %+v", resp.Result)
	}
}

func TestPodAdmissionResourceMismatch(t *testing.T) {
	rr := reservedRequest("r", 500, 1<<28, 60)
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	resp := handle(t, newAdmission(t, c), podFor("p", "r", 400, 1<<28))
	if resp.Allowed || !strings.Contains(resp.Result.Message, "do not match") {
		t.Fatalf("want denial about mismatch, got %+v", resp.Result)
	}
}

func TestPodAdmissionBindsReservation(t *testing.T) {
	rr := reservedRequest("r", 500, 1<<28, 60)
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	resp := handle(t, newAdmission(t, c), podFor("p", "r", 500, 1<<28))
	if !resp.Allowed {
		t.Fatalf("want allowed, got %+v", resp.Result)
	}
	var got quotav1alpha1.ResourceRequest
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "ns", Name: "r"}, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != quotav1alpha1.PhaseBound || got.Status.BoundPod != "p" {
		t.Fatalf("status after bind = %+v", got.Status)
	}
}

func TestPodAdmissionExpired(t *testing.T) {
	rr := reservedRequest("r", 500, 1<<28, 60)
	past := metav1.NewTime(time.Now().Add(-time.Minute))
	rr.Status.ExpiresAt = &past
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	resp := handle(t, newAdmission(t, c), podFor("p", "r", 500, 1<<28))
	if resp.Allowed || !strings.Contains(resp.Result.Message, "expired") {
		t.Fatalf("want denial about expiry, got %+v", resp.Result)
	}
}

func TestPodAdmissionAlreadyBound(t *testing.T) {
	rr := reservedRequest("r", 500, 1<<28, 60)
	rr.Status.Phase = quotav1alpha1.PhaseBound
	rr.Status.BoundPod = "other-pod"
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	resp := handle(t, newAdmission(t, c), podFor("p", "r", 500, 1<<28))
	if resp.Allowed || !strings.Contains(resp.Result.Message, "already bound") {
		t.Fatalf("want denial about double binding, got %+v", resp.Result)
	}

	// Same pod retrying its own admission is idempotently allowed.
	rr2 := reservedRequest("r2", 500, 1<<28, 60)
	rr2.Status.Phase = quotav1alpha1.PhaseBound
	rr2.Status.BoundPod = "p"
	c2 := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr2).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	resp2 := handle(t, newAdmission(t, c2), podFor("p", "r2", 500, 1<<28))
	if !resp2.Allowed {
		t.Fatalf("retried admission of the same pod must be allowed, got %+v", resp2.Result)
	}
}

func TestPodAdmissionRetriesOnConflict(t *testing.T) {
	rr := reservedRequest("r", 500, 1<<28, 60)
	base := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(rr).WithStatusSubresource(&quotav1alpha1.ResourceRequest{}).Build()
	failures := &atomic.Int32{}
	failures.Store(2) // fail the first two status updates with Conflict
	c := &flakyClient{Client: base, failures: failures}

	resp := handle(t, newAdmission(t, c), podFor("p", "r", 500, 1<<28))
	if !resp.Allowed {
		t.Fatalf("want allowed after conflict retries, got %+v", resp.Result)
	}
	var got quotav1alpha1.ResourceRequest
	if err := base.Get(context.Background(), types.NamespacedName{Namespace: "ns", Name: "r"}, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != quotav1alpha1.PhaseBound {
		t.Fatalf("phase = %s, want Bound", got.Status.Phase)
	}
}

func TestPodRequests(t *testing.T) {
	pod := &corev1.Pod{Spec: corev1.PodSpec{
		InitContainers: []corev1.Container{{
			Name: "init",
			Resources: corev1.ResourceRequirements{Requests: corev1.ResourceList{
				corev1.ResourceCPU:    *resource.NewMilliQuantity(900, resource.DecimalSI),
				corev1.ResourceMemory: *resource.NewQuantity(1<<29, resource.BinarySI),
			}},
		}},
		Containers: []corev1.Container{
			{Name: "a", Resources: corev1.ResourceRequirements{Requests: corev1.ResourceList{
				corev1.ResourceCPU:    *resource.NewMilliQuantity(300, resource.DecimalSI),
				corev1.ResourceMemory: *resource.NewQuantity(1<<28, resource.BinarySI),
			}}},
			{Name: "b", Resources: corev1.ResourceRequirements{Requests: corev1.ResourceList{
				corev1.ResourceCPU:    *resource.NewMilliQuantity(200, resource.DecimalSI),
				corev1.ResourceMemory: *resource.NewQuantity(1<<28, resource.BinarySI),
			}}},
		},
	}}
	cpu, mem := PodRequests(pod)
	// Scheduler formula: max(sum(containers), max(init)) per resource.
	if cpu != 900 || mem != 1<<29 {
		t.Fatalf("PodRequests = %dm/%dB, want 900m/%dB", cpu, mem, 1<<29)
	}
}
