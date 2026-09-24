// Package webhook contains the pod admission handler that binds pods to
// ResourceRequest reservations.
package webhook

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	admissionv1 "k8s.io/api/admission/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
)

// PodValidatePath is the HTTP path the pod validating webhook is served on.
const PodValidatePath = "/validate-v1-pod"

var (
	errReservationExpired = errors.New("reservation expired")
	errNotBindable        = errors.New("reservation not bindable")
)

// PodAdmission is a validating webhook that atomically binds a pod to its
// ResourceRequest. The Reserved -> Bound transition happens here, inside a
// resourceVersion-checked update retried on conflict: exactly one actor (this
// webhook or the controller's expiry path) can win the transition, so a pod
// can never consume an expired reservation and an expiry can never release
// quota out from under an admitted pod.
type PodAdmission struct {
	// Client must be a direct (uncached) client: admission decisions need
	// fresh reads and conflict-checked writes.
	Client  client.Client
	Decoder admission.Decoder
	// Now returns the current time; replaceable in tests. Nil means time.Now.
	Now func() time.Time
}

func (a *PodAdmission) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}

// Handle implements admission.Handler.
func (a *PodAdmission) Handle(ctx context.Context, req admission.Request) admission.Response {
	if req.Operation != admissionv1.Create {
		return admission.Allowed("only CREATE is validated")
	}
	pod := &corev1.Pod{}
	if err := a.Decoder.Decode(req, pod); err != nil {
		return admission.Errored(http.StatusBadRequest, err)
	}

	reqName := pod.Labels[quotav1alpha1.LabelRequest]
	if reqName == "" {
		return admission.Denied(fmt.Sprintf(
			"pods in this namespace must carry label %q referencing a ResourceRequest",
			quotav1alpha1.LabelRequest))
	}
	key := types.NamespacedName{Namespace: req.Namespace, Name: reqName}

	var rr quotav1alpha1.ResourceRequest
	if err := a.Client.Get(ctx, key, &rr); err != nil {
		if apierrors.IsNotFound(err) {
			return admission.Denied(fmt.Sprintf(
				"ResourceRequest %q not found in namespace %q", reqName, req.Namespace))
		}
		return admission.Errored(http.StatusInternalServerError, err)
	}
	if !rr.DeletionTimestamp.IsZero() {
		return admission.Denied(fmt.Sprintf("ResourceRequest %q is being deleted", reqName))
	}

	// The pod must consume exactly what was reserved — this is what keeps
	// the ledger, the request and the real object consistent.
	cpuMilli, memBytes := PodRequests(pod)
	if cpuMilli != rr.Spec.CPUMilli || memBytes != rr.Spec.MemoryBytes {
		return admission.Denied(fmt.Sprintf(
			"pod resource requests (cpu=%dm, memory=%dB) do not match reservation %q (cpu=%dm, memory=%dB)",
			cpuMilli, memBytes, reqName, rr.Spec.CPUMilli, rr.Spec.MemoryBytes))
	}

	podName := pod.Name
	if podName == "" {
		podName = pod.GenerateName + "*"
	}

	err := retry.RetryOnConflict(retry.DefaultBackoff, func() error {
		var latest quotav1alpha1.ResourceRequest
		if err := a.Client.Get(ctx, key, &latest); err != nil {
			return err
		}
		switch latest.Status.Phase {
		case quotav1alpha1.PhaseReserved:
			if latest.Status.ExpiresAt != nil && !a.now().Before(latest.Status.ExpiresAt.Time) {
				return errReservationExpired
			}
			latest.Status.Phase = quotav1alpha1.PhaseBound
			latest.Status.Reason = "PodBound"
			latest.Status.Message = fmt.Sprintf("pod %q consumed the reservation", podName)
			latest.Status.BoundPod = podName
			latest.Status.ObservedGeneration = latest.Generation
			return a.Client.Status().Update(ctx, &latest)
		case quotav1alpha1.PhaseBound:
			if latest.Status.BoundPod == podName {
				return nil // same admission retried (e.g. client timeout)
			}
			return fmt.Errorf("%w: already bound to pod %q", errNotBindable, latest.Status.BoundPod)
		default:
			phase := latest.Status.Phase
			if phase == "" {
				phase = quotav1alpha1.PhasePending
			}
			return fmt.Errorf("%w: phase is %q", errNotBindable, phase)
		}
	})
	switch {
	case err == nil:
		return admission.Allowed("reservation bound to pod")
	case errors.Is(err, errReservationExpired):
		return admission.Denied(
			"reservation expired before the pod arrived; create a new ResourceRequest")
	case errors.Is(err, errNotBindable):
		return admission.Denied(err.Error())
	default:
		return admission.Errored(http.StatusInternalServerError, err)
	}
}

// PodRequests computes the effective resource request of a pod the same way
// the Kubernetes scheduler does: per resource, max(sum of app containers,
// largest init container).
func PodRequests(pod *corev1.Pod) (cpuMilli, memBytes int64) {
	var sumCPU, sumMem, maxInitCPU, maxInitMem int64
	for _, c := range pod.Spec.Containers {
		sumCPU += c.Resources.Requests.Cpu().MilliValue()
		sumMem += c.Resources.Requests.Memory().Value()
	}
	for _, c := range pod.Spec.InitContainers {
		if v := c.Resources.Requests.Cpu().MilliValue(); v > maxInitCPU {
			maxInitCPU = v
		}
		if v := c.Resources.Requests.Memory().Value(); v > maxInitMem {
			maxInitMem = v
		}
	}
	return max(sumCPU, maxInitCPU), max(sumMem, maxInitMem)
}
