// Package v1alpha1 contains the Snapshot API types.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SnapshotPhase is the high-level state of a Snapshot.
// +kubebuilder:validation:Enum=Pending;Running;Ready;Failed
type SnapshotPhase string

const (
	// PhasePending means no worker Job for the current generation exists yet.
	PhasePending SnapshotPhase = "Pending"
	// PhaseRunning means the worker Job for the current generation is in flight.
	PhaseRunning SnapshotPhase = "Running"
	// PhaseReady means the worker Job completed successfully and a digest was recorded.
	PhaseReady SnapshotPhase = "Ready"
	// PhaseFailed means the worker Job failed and will not be retried for this generation.
	PhaseFailed SnapshotPhase = "Failed"
)

// SnapshotSpec defines the desired state of a Snapshot.
type SnapshotSpec struct {
	// SourceConfigMap is the ConfigMap whose keys are snapshotted and digested.
	// +kubebuilder:validation:MinLength=1
	SourceConfigMap string `json:"sourceConfigMap"`

	// SubPath optionally restricts the digest to a single key of the source
	// ConfigMap. Empty means every key is included. A non-existent key makes
	// the generation fail deterministically (the worker Job exits non-zero).
	// +optional
	SubPath string `json:"subPath,omitempty"`
}

// JobRef points at the worker Job owned by a generation.
type JobRef struct {
	// Name of the worker Job.
	Name string `json:"name"`
	// Generation the Job was created for.
	Generation int64 `json:"generation"`
}

// SnapshotStatus defines the observed state of a Snapshot.
type SnapshotStatus struct {
	// ObservedGeneration is the .metadata.generation the current status reflects.
	// Status of older generations must never overwrite status for newer ones.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Phase is the current lifecycle phase.
	// +optional
	Phase SnapshotPhase `json:"phase,omitempty"`

	// Digest is the SHA-256 hex digest of the canonicalised source content.
	// +optional
	Digest string `json:"digest,omitempty"`

	// Algorithm is the digest algorithm used, e.g. "SHA-256".
	// +optional
	Algorithm string `json:"algorithm,omitempty"`

	// FileCount is the number of digested keys/files.
	// +optional
	FileCount int `json:"fileCount,omitempty"`

	// TotalBytes is the sum of sizes of the digested values.
	// +optional
	TotalBytes int64 `json:"totalBytes,omitempty"`

	// Manifest is the deterministic listing that was hashed (for auditing).
	// +optional
	Manifest string `json:"manifest,omitempty"`

	// JobRef identifies the worker Job for ObservedGeneration.
	// +optional
	JobRef *JobRef `json:"jobRef,omitempty"`

	// FailureReason is a short machine-readable failure cause.
	// +optional
	FailureReason string `json:"failureReason,omitempty"`

	// FailureMessage is a human-readable failure description.
	// +optional
	FailureMessage string `json:"failureMessage,omitempty"`

	// Conditions holds condition state, mirroring the phase transitions.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// Snapshot is the Schema for the snapshots API.
type Snapshot struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SnapshotSpec   `json:"spec,omitempty"`
	Status SnapshotStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// SnapshotList contains a list of Snapshot.
type SnapshotList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Snapshot `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Snapshot{}, &SnapshotList{})
}
