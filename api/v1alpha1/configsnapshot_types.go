package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// ConfigSnapshotSpec describes an immutable piece of configuration that is
// delivered to a set of target namespaces.
//
// The payload is immutable by contract: once the object is created, Payload
// must not change (the validation webhook enforces this). Changing content
// means creating a new ConfigSnapshot. The selector MAY change: narrowing it
// causes owned children in namespaces that left the selection to be
// garbage-collected.
type ConfigSnapshotSpec struct {
	// Payload is the immutable configuration content distributed to every
	// target namespace.
	// +kubebuilder:validation:Required
	Payload Payload `json:"payload"`

	// Selector selects target namespaces by labels. It may be updated after
	// creation; a narrower selector removes children from namespaces that are
	// no longer selected (only children owned by this object's UID).
	// +kubebuilder:validation:Required
	Selector metav1.LabelSelector `json:"selector"`
}

// Payload is the configuration content. Only one format is supported today.
type Payload struct {
	// Format is the payload format, for example "properties" (Java .properties)
	// or "json". It is written verbatim into every ConfigMap.
	// +kubebuilder:default="properties"
	// +kubebuilder:validation:Enum=properties;json;text
	Format string `json:"format,omitempty"`

	// Data is the textual configuration content. It must be non-empty.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Data string `json:"data"`
}

// TargetPhase is the per-target delivery phase.
// +kubebuilder:validation:Enum=Pending;Applied;Failed
type TargetPhase string

const (
	// TargetPending means the target has been observed but not reconciled yet.
	TargetPending TargetPhase = "Pending"
	// TargetApplied means the child object exists and carries the desired content.
	TargetApplied TargetPhase = "Applied"
	// TargetFailed means the last apply attempt returned an error.
	TargetFailed TargetPhase = "Failed"
)

// TargetStatus records the delivery state for one target namespace.
type TargetStatus struct {
	// Namespace is the target namespace name.
	Namespace string `json:"namespace"`
	// UID is the UID of the target namespace observed during the last select.
	// +optional
	UID types.UID `json:"uid,omitempty"`
	// Phase is Pending, Applied or Failed.
	Phase TargetPhase `json:"phase"`
	// Version is the content version (sha256 digest of the canonical payload)
	// currently applied to this target. It is only advanced on success and is
	// never rolled back when another target fails.
	// +optional
	Version string `json:"version,omitempty"`
	// ChildName is the name of the child object in the target namespace.
	// +optional
	ChildName string `json:"childName,omitempty"`
	// ObservedGeneration is the ConfigSnapshot generation that this status
	// entry reflects.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// LastError is the last apply error, if any.
	// +optional
	LastError string `json:"lastError,omitempty"`
	// LastTransitionTime is when the phase last changed.
	// +optional
	LastTransitionTime metav1.Time `json:"lastTransitionTime,omitempty"`
}

// ConfigSnapshotStatus defines the observed state of ConfigSnapshot.
type ConfigSnapshotStatus struct {
	// ObservedGeneration is the generation last fully reconciled into the
	// target list.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// Version is the sha256 digest of the canonical payload.
	// +optional
	Version string `json:"version,omitempty"`
	// TargetCount is the number of currently selected target namespaces.
	// +optional
	TargetCount int `json:"targetCount,omitempty"`
	// AppliedCount is the number of targets in phase Applied.
	// +optional
	AppliedCount int `json:"appliedCount,omitempty"`
	// FailedCount is the number of targets in phase Failed.
	// +optional
	FailedCount int `json:"failedCount,omitempty"`
	// Targets is the per-target status, sorted by namespace name.
	// +optional
	Targets []TargetStatus `json:"targets,omitempty"`
	// Conditions follow the usual Kubernetes conventions.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

const (
	// ConditionReady is True when every selected target is Applied, False while
	// any target is Failed or Pending.
	ConditionReady = "Ready"
	// ConditionReconciling is True while a reconcile round is in flight.
	ConditionReconciling = "Reconciling"
)

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=csnap
// +kubebuilder:printcolumn:name="Version",type=string,JSONPath=`.status.version`
// +kubebuilder:printcolumn:name="Targets",type=integer,JSONPath=`.status.targetCount`
// +kubebuilder:printcolumn:name="Applied",type=integer,JSONPath=`.status.appliedCount`
// +kubebuilder:printcolumn:name="Failed",type=integer,JSONPath=`.status.failedCount`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ConfigSnapshot distributes an immutable piece of configuration to every
// namespace matching a label selector.
type ConfigSnapshot struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ConfigSnapshotSpec   `json:"spec,omitempty"`
	Status ConfigSnapshotStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ConfigSnapshotList contains a list of ConfigSnapshot.
type ConfigSnapshotList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ConfigSnapshot `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ConfigSnapshot{}, &ConfigSnapshotList{})
}
