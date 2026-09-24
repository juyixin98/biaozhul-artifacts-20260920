package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Phase is the lifecycle phase of a ResourceRequest.
//
// State machine (all transitions are explicit and guarded by resourceVersion):
//
//	Pending  --reserve ok-->  Reserved --pod admitted--> Bound   --pod done/deleted--> Released
//	Pending  --no capacity-->  Rejected (terminal)
//	Reserved --TTL elapsed-->  Expired  (terminal; late pods are rejected at admission
//	                                   and swept by the controller if they slip through)
type Phase string

const (
	// PhasePending means the request was accepted and waits for quota reservation.
	PhasePending Phase = "Pending"
	// PhaseReserved means quota is reserved in the pool ledger; waiting for the Pod.
	PhaseReserved Phase = "Reserved"
	// PhaseBound means a Pod consumed the reservation (admission-time transition).
	PhaseBound Phase = "Bound"
	// PhaseExpired means the TTL elapsed before any Pod arrived; quota released. Terminal.
	PhaseExpired Phase = "Expired"
	// PhaseRejected means pool capacity was insufficient; nothing was reserved. Terminal.
	PhaseRejected Phase = "Rejected"
	// PhaseReleased means the bound Pod finished or was deleted; quota released. Terminal.
	PhaseReleased Phase = "Released"
)

const (
	// LabelRequest is the pod label naming the owning ResourceRequest.
	LabelRequest = "quota.biaozhu.dev/request"
	// LabelManagedBy marks namespaces where the admission webhooks apply.
	LabelManagedBy = "quota.biaozhu.dev/managed-by"
	// ManagedByValue is the expected value of LabelManagedBy.
	ManagedByValue = "quota-reserver"
	// Finalizer ensures reserved quota is released before the object disappears.
	Finalizer = "quota.biaozhu.dev/release-quota"
)

// ResourceRequestSpec describes one quota reservation request.
// All quantities are plain integers in fixed units — no suffix parsing:
// CPU in milli-cores, memory in bytes, TTL in seconds.
type ResourceRequestSpec struct {
	// Pool is the name of the QuotaPool in the same namespace to reserve from.
	// +kubebuilder:default="default"
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	// +optional
	Pool string `json:"pool,omitempty"`

	// CPUMilli is the requested CPU in milli-cores (500 = 0.5 cores).
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=100000
	CPUMilli int64 `json:"cpuMilli"`

	// MemoryBytes is the requested memory in bytes.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=109951162777600
	MemoryBytes int64 `json:"memoryBytes"`

	// TTLSeconds is how long the reservation waits for its Pod before expiring.
	// +kubebuilder:validation:Minimum=10
	// +kubebuilder:validation:Maximum=86400
	TTLSeconds int64 `json:"ttlSeconds"`
}

// ResourceRequestStatus is the observed state of a ResourceRequest.
type ResourceRequestStatus struct {
	// Phase is the current lifecycle phase.
	// +optional
	Phase Phase `json:"phase,omitempty"`

	// Reason is a short CamelCase machine-readable explanation of the phase.
	// +optional
	Reason string `json:"reason,omitempty"`

	// Message is a human-readable explanation of the phase.
	// +optional
	Message string `json:"message,omitempty"`

	// ReservedAt is when the quota reservation was recorded in the pool ledger.
	// +optional
	ReservedAt *metav1.Time `json:"reservedAt,omitempty"`

	// ExpiresAt is when the reservation expires if no Pod has consumed it.
	// +optional
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`

	// BoundPod is the name of the Pod that consumed the reservation.
	// +optional
	BoundPod string `json:"boundPod,omitempty"`

	// ObservedGeneration is the latest spec generation reconciled.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=rrq
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="CPU(m)",type=integer,JSONPath=`.spec.cpuMilli`
// +kubebuilder:printcolumn:name="Mem(B)",type=integer,JSONPath=`.spec.memoryBytes`
// +kubebuilder:printcolumn:name="TTL(s)",type=integer,JSONPath=`.spec.ttlSeconds`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ResourceRequest is the Schema for the resourcerequests API.
type ResourceRequest struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ResourceRequestSpec   `json:"spec,omitempty"`
	Status ResourceRequestStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ResourceRequestList contains a list of ResourceRequest.
type ResourceRequestList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ResourceRequest `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ResourceRequest{}, &ResourceRequestList{})
}
