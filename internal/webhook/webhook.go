// Package webhook contains the validating admission webhooks for the
// reservation system.
//
// Two webhooks are required to make the invariants enforceable:
//
//   - validate-resourceclaim: rejects bad units/units/TTL at write time and
//     makes spec immutable after creation (keeps the pool ledger simple and
//     makes the optimistic-concurrency story precise).
//   - validate-pod: a Pod carrying the claim label must reference an existing
//     Reserved claim with enough capacity and an unexpired TTL. This is the
//     *only* place a "late Pod" (created after the controller released the
//     reservation) can be stopped deterministically. The controller still
//     contains a late-pod rule for the narrow window between expiry decision
//     and API-server admission propagation.
package webhook

import (
	"context"
	"fmt"
	"net/http"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	quota "resourcequota-reservation/api/v1alpha1"
	"resourcequota-reservation/internal/ledger"
)

// ClaimValidator validates ResourceClaim CREATE/UPDATE.
type ClaimValidator struct {
	Client  client.Client
	Decoder admission.Decoder
}

// Handle implements admission.Handler.
func (v *ClaimValidator) Handle(ctx context.Context, req admission.Request) admission.Response {
	var claim quota.ResourceClaim
	if err := v.Decoder.Decode(req, &claim); err != nil {
		return admission.Errored(http.StatusBadRequest, err)
	}

	if _, _, err := ledger.ValidateClaimSpec(claim.Spec); err != nil {
		return admission.Denied(err.Error())
	}

	if req.Operation == "UPDATE" {
		var old quota.ResourceClaim
		if err := v.Decoder.DecodeRaw(req.OldObject, &old); err != nil {
			return admission.Errored(http.StatusBadRequest, err)
		}
		// Spec is fully immutable: reservations are a debit contract with the
		// pool; changing it would require a refund/re-debit protocol that adds
		// ambiguity. Users create a new claim instead.
		if old.Spec.CPU != claim.Spec.CPU ||
			old.Spec.Memory != claim.Spec.Memory ||
			old.Spec.TTL.Duration != claim.Spec.TTL.Duration {
			return admission.Denied("resourceclaim spec (cpu, memory, ttl) is immutable after creation")
		}
	}
	return admission.Allowed("")
}

// PodValidator validates Pods that reference a claim.
type PodValidator struct {
	Client  client.Client
	Decoder admission.Decoder
	// Now is overridable in tests.
	Now func() time.Time
}

// Handle implements admission.Handler.
func (v *PodValidator) Handle(ctx context.Context, req admission.Request) admission.Response {
	logger := log.FromContext(ctx)

	var pod corev1.Pod
	if err := v.Decoder.Decode(req, &pod); err != nil {
		return admission.Errored(http.StatusBadRequest, err)
	}
	claimName := pod.Labels[quota.ClaimLabelKey]
	if claimName == "" {
		return admission.Allowed("") // unmanaged pod; nothing to enforce
	}

	var claim quota.ResourceClaim
	err := v.Client.Get(ctx, types.NamespacedName{Namespace: req.Namespace, Name: claimName}, &claim)
	if err != nil {
		return admission.Denied(fmt.Sprintf("claim %q not found: %v", claimName, err))
	}

	now := time.Now()
	if v.Now != nil {
		now = v.Now()
	}

	switch claim.Status.Phase {
	case quota.PhaseReserved:
		// allowed window; TTL re-check guards against skew between the
		// controller's requeue and the admission timestamp.
		if claim.Status.ExpiresAt != nil && !now.Before(claim.Status.ExpiresAt.Time) {
			return admission.Denied(fmt.Sprintf("claim %q expired at %s; late pods are not admitted",
				claimName, claim.Status.ExpiresAt.Time.Format(time.RFC3339)))
		}
	case quota.PhaseBound:
		// Only one pod per claim; a second pod referencing it is denied so the
		// reservation cannot be silently overspent.
		return admission.Denied(fmt.Sprintf("claim %q is already bound to pod %q", claimName, claim.Status.BoundPod))
	case quota.PhasePending, "":
		return admission.Denied(fmt.Sprintf("claim %q is not reserved yet (phase=%q)", claimName, claim.Status.Phase))
	case quota.PhaseExpired:
		return admission.Denied(fmt.Sprintf("claim %q has expired and its capacity was released", claimName))
	case quota.PhaseRejected:
		return admission.Denied(fmt.Sprintf("claim %q was rejected: %s", claimName, claim.Status.Message))
	default:
		return admission.Denied(fmt.Sprintf("claim %q in unexpected phase %q", claimName, claim.Status.Phase))
	}

	// Resource conformance: the pod's requests must fit inside the reservation.
	podCPU, podMem := ledger.PodRequests(&pod)
	claimCPU, err := ledger.ParseCPU(claim.Spec.CPU)
	if err != nil {
		return admission.Errored(http.StatusInternalServerError, err)
	}
	claimMem, err := ledger.ParseMemory(claim.Spec.Memory)
	if err != nil {
		return admission.Errored(http.StatusInternalServerError, err)
	}
	if podCPU > claimCPU {
		return admission.Denied(fmt.Sprintf("pod requests %dm cpu but claim reserves %dm", podCPU, claimCPU))
	}
	if podMem > claimMem {
		return admission.Denied(fmt.Sprintf("pod requests %d bytes memory but claim reserves %d", podMem, claimMem))
	}

	// A pod must actually request SOMETHING from the reserved resources; an
	// unbounded best-effort pod labeled with a claim would hide consumption.
	if podCPU == 0 && podMem == 0 {
		return admission.Denied("pod bound to a claim must declare cpu/memory requests")
	}

	logger.Info("admitted pod against claim", "claim", claimName, "pod", pod.Name, "cpu", podCPU, "mem", podMem)
	return admission.Allowed("")
}
