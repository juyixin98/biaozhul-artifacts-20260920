// Package v1alpha1 contains API Schema definitions for the quota v1alpha1 API group.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// GroupVersion is group version used to register these objects.
var GroupVersion = schema.GroupVersion{Group: "quota.example.com", Version: "v1alpha1"}

// SchemeGroupVersion is an alias kept for clarity in tests.
var SchemeGroupVersion = GroupVersion

var (
	// SchemeBuilder collects functions that add types to a scheme.
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	// AddToScheme registers the API types with a runtime.Scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)

// Resource takes an unqualified resource and returns a Group-qualified GroupResource.
func Resource(resource string) schema.GroupResource {
	return GroupVersion.WithResource(resource).GroupResource()
}

func addKnownTypes(scheme *runtime.Scheme) error {
	scheme.AddKnownTypes(GroupVersion,
		&ResourceClaim{},
		&ResourceClaimList{},
		&ReservationPool{},
		&ReservationPoolList{},
	)
	metav1.AddToGroupVersion(scheme, GroupVersion)
	return nil
}

// ClaimPhase is the lifecycle state of a ResourceClaim.
//
// State machine:
//
//	Pending -> Reserved -> Bound
//	  |          |  ^        |
//	  v          v  |        v
//	Rejected   Expired ------  (Expired -> Reserved is never allowed;
//	           Expired -> Bound happens only through the LatePod rule when
//	           an admitted Pod is discovered, see controller documentation)
//	  |
//	  +-> Released (claim finalizer removed; terminal, object may be deleted)
type ClaimPhase string

const (
	// PhasePending means the claim has been admitted by the webhook but the
	// controller has not yet reserved capacity.
	PhasePending ClaimPhase = "Pending"
	// PhaseReserved means capacity has been debited from the ReservationPool;
	// no Pod bound to the claim exists yet.
	PhaseReserved ClaimPhase = "Reserved"
	// PhaseBound means a Pod carrying the claim label is running/exists and
	// the reservation is now backed by a real object.
	PhaseBound ClaimPhase = "Bound"
	// PhaseExpired means the TTL elapsed while the claim was still Reserved.
	// Capacity has been credited back to the pool. Pods created after this
	// transition are rejected by the validating webhook; a Pod that slips in
	// during the release window moves the claim to Bound via LatePod.
	PhaseExpired ClaimPhase = "Expired"
	// PhaseRejected means reservation was impossible (insufficient capacity or
	// conflict budget exhausted) and nothing was debited.
	PhaseRejected ClaimPhase = "Rejected"
)

const (
	// ClaimFinalizer ensures the controller can return capacity to the pool
	// before a ResourceClaim object disappears.
	ClaimFinalizer = "quota.example.com/claim-finalizer"

	// ClaimLabelKey is placed on Pods ("quota.example.com/claim": <claim-name>)
	// to bind a Pod to a ResourceClaim.
	ClaimLabelKey = "quota.example.com/claim"

	// PoolName is the fixed name of the per-namespace ReservationPool object.
	// One pool per namespace keeps the optimistic-concurrency contention
	// domain small and matches Kubernetes' namespace-scoped ResourceQuota.
	PoolName = "default-pool"
)

// ResourceClaimSpec describes a request for CPU and memory capacity with a TTL.
type ResourceClaimSpec struct {
	// CPU is the requested amount of CPU, in Kubernetes resource.Quantity
	// notation restricted to milli-cpu, e.g. "500m", "1" (=1000m). Range 1m..64000m.
	// +kubebuilder:validation:Required
	CPU string `json:"cpu"`
	// Memory is the requested amount of memory, e.g. "128Mi", "1Gi". Range 1Mi..256Gi.
	// +kubebuilder:validation:Required
	Memory string `json:"memory"`
	// TTL is the lifetime of the reservation while unbound, e.g. "30s", "5m",
	// "2h". Range 1s..24h. It starts counting when the claim is created.
	// +kubebuilder:validation:Required
	TTL metav1.Duration `json:"ttl"`
}

// ReservationStatus is the bookkeeping of one debit against the pool.
type ReservationStatus struct {
	// Phase is the current lifecycle state.
	// +kubebuilder:validation:Required
	Phase ClaimPhase `json:"phase"`
	// ReservedCPU / ReservedMemory echo the quantities that were debited,
	// normalized by the API server's quantity parser.
	// +optional
	ReservedCPU string `json:"reservedCPU,omitempty"`
	// +optional
	ReservedMemory string `json:"reservedMemory,omitempty"`
	// ReservedAt is when capacity was debited.
	// +optional
	ReservedAt *metav1.Time `json:"reservedAt,omitempty"`
	// ExpiresAt is ReservedAt + TTL. The controller releases the claim once
	// this instant is in the past and no bound Pod exists.
	// +optional
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`
	// BoundPod is the name of the Pod backing this reservation, if any.
	// +optional
	BoundPod string `json:"boundPod,omitempty"`
	// ConflictCount counts resource-version conflicts hit while debiting the
	// pool; surfaced to demonstrate optimistic concurrency in tests.
	// +optional
	ConflictCount int32 `json:"conflictCount,omitempty"`
	// Message carries a human-readable reason for Rejected/Expired states.
	// +optional
	Message string `json:"message,omitempty"`
	// ObservedGeneration is the .metadata.generation last reconciled.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// ResourceClaim is a request to reserve CPU/memory capacity in a namespace.
// +kubebuilder:resource:shortName=rc;rclaim
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="CPU",type=string,JSONPath=`.spec.cpu`
// +kubebuilder:printcolumn:name="Memory",type=string,JSONPath=`.spec.memory`
// +kubebuilder:printcolumn:name="TTL",type=string,JSONPath=`.spec.ttl`
// +kubebuilder:printcolumn:name="BoundPod",type=string,JSONPath=`.status.boundPod`
// +kubebuilder:printcolumn:name="ExpiresAt",type=date,JSONPath=`.status.expiresAt`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type ResourceClaim struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ResourceClaimSpec `json:"spec,omitempty"`
	Status ReservationStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ResourceClaimList contains a list of ResourceClaim.
type ResourceClaimList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ResourceClaim `json:"items"`
}

// PoolSpec defines the total capacity that can be reserved in a namespace.
type PoolSpec struct {
	// CPUCapacity is the total milli-cpu budget, e.g. "8000m".
	// +kubebuilder:validation:Required
	CPUCapacity string `json:"cpuCapacity"`
	// MemoryCapacity is the total memory budget, e.g. "16Gi".
	// +kubebuilder:validation:Required
	MemoryCapacity string `json:"memoryCapacity"`
}

// PoolStatus shows what is currently reserved and by whom. It is the
// authoritative, durable counter — the source of truth is etcd plus the
// API server's optimistic-concurrency check, never an in-memory map.
type PoolStatus struct {
	// ReservedCPU / ReservedMemory are sums over active (Reserved|Bound) claims.
	// +optional
	ReservedCPU string `json:"reservedCPU,omitempty"`
	// +optional
	ReservedMemory string `json:"reservedMemory,omitempty"`
	// Claims maps claim UID -> "cpu,memory,phase,name" for idempotent debit/credit
	// across requeues and controller restarts.
	// +optional
	Claims map[string]PoolClaimEntry `json:"claims,omitempty"`
}

// PoolClaimEntry is one claim's contribution to the pool.
type PoolClaimEntry struct {
	// Name of the ResourceClaim (for human inspection).
	Name string `json:"name"`
	// CPU contributed in milli-cpu integer form.
	CPU int64 `json:"cpu"`
	// Memory contributed in bytes.
	Memory int64 `json:"memory"`
	// Active is true while the claim is Reserved or Bound. Expired/Rejected
	// entries are deleted on the next credit.
	Active bool `json:"active"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// ReservationPool is the per-namespace capacity ledger.
// +kubebuilder:resource:shortName=rpool
// +kubebuilder:printcolumn:name="CPUCap",type=string,JSONPath=`.spec.cpuCapacity`
// +kubebuilder:printcolumn:name="MemCap",type=string,JSONPath=`.spec.memoryCapacity`
// +kubebuilder:printcolumn:name="CPUUsed",type=string,JSONPath=`.status.reservedCPU`
// +kubebuilder:printcolumn:name="MemUsed",type=string,JSONPath=`.status.reservedMemory`
type ReservationPool struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   PoolSpec   `json:"spec,omitempty"`
	Status PoolStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ReservationPoolList contains a list of ReservationPool.
type ReservationPoolList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ReservationPool `json:"items"`
}

// DeepCopy methods are generated into zz_generated.deepcopy.go.

func init() {}
