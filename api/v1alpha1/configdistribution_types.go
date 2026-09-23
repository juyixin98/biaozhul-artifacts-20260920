// Package v1alpha1 contains API Schema definitions for the dist v1alpha1 API group.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ConfigDistributionSpec defines the desired state of ConfigDistribution.
type ConfigDistributionSpec struct {
	// Config is the immutable configuration payload distributed to every
	// selected namespace as a child ConfigMap. It cannot be changed after
	// creation; create a new ConfigDistribution to roll out new content.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinProperties=1
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="config is immutable; create a new ConfigDistribution instead"
	Config map[string]string `json:"config"`

	// NamespaceSelector selects the namespaces that receive a copy of Config.
	// An empty selector matches no namespaces; use matchLabels: {} semantics via
	// an empty LabelSelector to match all namespaces.
	// +kubebuilder:validation:Required
	NamespaceSelector metav1.LabelSelector `json:"namespaceSelector"`
}

// TargetPhase is the per-namespace rollout state.
// +kubebuilder:validation:Enum=Ready;Failed;Conflict
type TargetPhase string

const (
	// TargetReady means the child ConfigMap exists with the current digest.
	TargetReady TargetPhase = "Ready"
	// TargetFailed means the last attempt for this namespace failed and will
	// be retried; other targets are unaffected.
	TargetFailed TargetPhase = "Failed"
	// TargetConflict means a same-name object exists that is not owned by
	// this resource; it is left untouched and reported.
	TargetConflict TargetPhase = "Conflict"
)

// TargetStatus records the rollout state of one target namespace.
type TargetStatus struct {
	// Namespace is the target namespace name.
	Namespace string `json:"namespace"`
	// Digest is the config version (sha256 hex) applied to this namespace.
	Digest string `json:"digest"`
	// Phase is Ready, Failed or Conflict.
	Phase TargetPhase `json:"phase"`
	// Message holds failure/conflict evidence for this target.
	// +optional
	Message string `json:"message,omitempty"`
	// LastTransitionTime is when this entry last changed phase or digest.
	// +optional
	LastTransitionTime metav1.Time `json:"lastTransitionTime,omitempty"`
}

// ConfigDistributionStatus defines the observed state of ConfigDistribution.
type ConfigDistributionStatus struct {
	// ObservedGeneration is the latest generation reconciled.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// ConfigDigest is the sha256 hex digest of the canonicalized spec.Config.
	// Child objects are named after it, which makes rollout idempotent.
	// +optional
	ConfigDigest string `json:"configDigest,omitempty"`
	// Targets records the version applied to each selected namespace.
	// Successful targets are never rolled back when other targets fail.
	// +optional
	Targets []TargetStatus `json:"targets,omitempty"`
	// Conditions summarize the rollout (Ready / Degraded).
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster,shortName=cfds
// +kubebuilder:printcolumn:name="Digest",type=string,JSONPath=`.status.configDigest`,priority=1
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=`.status.conditions[?(@.type=="Ready")].status`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ConfigDistribution distributes one immutable config to selected namespaces.
type ConfigDistribution struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ConfigDistributionSpec   `json:"spec,omitempty"`
	Status ConfigDistributionStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ConfigDistributionList contains a list of ConfigDistribution.
type ConfigDistributionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ConfigDistribution `json:"items"`
}
