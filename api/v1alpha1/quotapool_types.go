package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// QuotaPoolSpec defines the total capacity of a quota pool.
type QuotaPoolSpec struct {
	// CPUMilli is the total CPU capacity in milli-cores (1000 = 1 core).
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=1000000
	CPUMilli int64 `json:"cpuMilli"`

	// MemoryBytes is the total memory capacity in bytes.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=109951162777600
	MemoryBytes int64 `json:"memoryBytes"`
}

// Allocation records one active reservation held against the pool. The ledger
// is keyed by the UID of the owning ResourceRequest, which makes reservation
// idempotent: re-reserving the same request is a no-op, never a double count.
type Allocation struct {
	// RequestUID is the UID of the ResourceRequest holding this allocation.
	RequestUID types.UID `json:"requestUID"`

	// RequestName is the name of the ResourceRequest (informational only;
	// the UID is the authoritative key).
	RequestName string `json:"requestName"`

	// CPUMilli reserved by this allocation.
	CPUMilli int64 `json:"cpuMilli"`

	// MemoryBytes reserved by this allocation.
	MemoryBytes int64 `json:"memoryBytes"`
}

// QuotaPoolStatus holds the allocation ledger and the aggregated usage.
// Used totals are always recomputed from Allocations, so the ledger is the
// single source of truth and the counters can never drift from it.
type QuotaPoolStatus struct {
	// Allocations is the authoritative ledger of active reservations.
	// +listType=map
	// +listMapKey=requestUID
	// +optional
	Allocations []Allocation `json:"allocations,omitempty"`

	// UsedCPUMilli is the sum of Allocations[].cpuMilli.
	// +optional
	UsedCPUMilli int64 `json:"usedCPUMilli"`

	// UsedMemoryBytes is the sum of Allocations[].memoryBytes.
	// +optional
	UsedMemoryBytes int64 `json:"usedMemoryBytes"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=qpool
// +kubebuilder:printcolumn:name="UsedCPU(m)",type=integer,JSONPath=`.status.usedCPUMilli`
// +kubebuilder:printcolumn:name="CapCPU(m)",type=integer,JSONPath=`.spec.cpuMilli`
// +kubebuilder:printcolumn:name="UsedMem(B)",type=integer,JSONPath=`.status.usedMemoryBytes`
// +kubebuilder:printcolumn:name="CapMem(B)",type=integer,JSONPath=`.spec.memoryBytes`

// QuotaPool is the Schema for the quotapools API. One pool (conventionally
// named "default") meters resource reservations inside one namespace.
type QuotaPool struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   QuotaPoolSpec   `json:"spec,omitempty"`
	Status QuotaPoolStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// QuotaPoolList contains a list of QuotaPool.
type QuotaPoolList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []QuotaPool `json:"items"`
}

func init() {
	SchemeBuilder.Register(&QuotaPool{}, &QuotaPoolList{})
}
