package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Phase is the state of a Snapshot's current generation.
// +kubebuilder:validation:Enum=Pending;Running;Ready;Failed
type Phase string

const (
	// PhasePending means the controller has not yet (re)started processing the
	// current generation of the Snapshot.
	PhasePending Phase = "Pending"
	// PhaseRunning means the snapshot-producing Job for the current generation
	// exists and has not reached a terminal state.
	PhaseRunning Phase = "Running"
	// PhaseReady means the Job completed successfully and the controller
	// verified the produced result artifact (real file digest), all bound to
	// .metadata.generation via observedGeneration.
	PhaseReady Phase = "Ready"
	// PhaseFailed means the Job exhausted its retries or produced an invalid
	// result artifact.
	PhaseFailed Phase = "Failed"
)

// SnapshotSource selects the local test resource whose payload becomes the
// snapshot input. Exactly one kind is supported: ConfigMap or Secret.
//
// +kubebuilder:object:generate=true
type SnapshotSource struct {
	// Kind of the source object: ConfigMap or Secret.
	// +kubebuilder:validation:Enum=ConfigMap;Secret
	// +kubebuilder:default=ConfigMap
	Kind string `json:"kind"`
	// Name of the source object in the Snapshot's namespace.
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// SnapshotSpec defines the desired state of Snapshot.
//
// +kubebuilder:object:generate=true
type SnapshotSpec struct {
	// Source selects the ConfigMap/Secret payload that the snapshot Job packs
	// and hashes.
	// +kubebuilder:validation:Required
	Source SnapshotSource `json:"source"`

	// OutputName is the base file name of the produced tar archive
	// (e.g. "data.tar.gz"). A .gz suffix enables gzip compression.
	// +kubebuilder:default=snapshot.tar.gz
	// +kubebuilder:validation:MinLength=1
	// +optional
	OutputName string `json:"outputName,omitempty"`

	// BackoffLimit mirrors Job.spec.backoffLimit: number of retries before the
	// Job is considered Failed.
	// +kubebuilder:default=2
	// +kubebuilder:validation:Minimum=0
	// +optional
	BackoffLimit *int32 `json:"backoffLimit,omitempty"`

	// TTLSecondsAfterFinished keeps the produced Job around for inspection
	// after completion; Kubernetes GC deletes it afterwards.
	// +kubebuilder:default=3600
	// +kubebuilder:validation:Minimum=0
	// +optional
	TTLSecondsAfterFinished *int32 `json:"ttlSecondsAfterFinished,omitempty"`
}

// ResultFile describes one file that was packed into the snapshot archive.
//
// +kubebuilder:object:generate=true
type ResultFile struct {
	// Path is the file name inside the archive.
	Path string `json:"path"`
	// Size is the uncompressed size in bytes.
	Size int64 `json:"size"`
	// SHA256 is the hex-encoded SHA-256 digest of the file content.
	SHA256 string `json:"sha256"`
}

// SnapshotStatus defines the observed state of Snapshot.
//
// +kubebuilder:object:generate=true
type SnapshotStatus struct {
	// Phase is the current state of the Snapshot (Pending/Running/Ready/Failed).
	// +optional
	Phase Phase `json:"phase,omitempty"`

	// ObservedGeneration is the .metadata.generation that the status
	// (including digest and phase) reflects. A stale Job reporting its result
	// back is discarded when its generation label does not equal this value.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// JobRef names the Job owned by the current generation.
	// +optional
	JobRef string `json:"jobRef,omitempty"`

	// ResultConfigMap names the ConfigMap published by the worker holding
	// result.json (digest, file list).
	// +optional
	ResultConfigMap string `json:"resultConfigMap,omitempty"`

	// SHA256 is the hex-encoded SHA-256 digest of the produced archive file.
	// It is only populated once the real archive was produced by the Job.
	// +optional
	SHA256 string `json:"sha256,omitempty"`

	// SizeBytes is the archive size in bytes.
	// +optional
	SizeBytes int64 `json:"sizeBytes,omitempty"`

	// Files lists the packed input files and their individual digests.
	// +optional
	Files []ResultFile `json:"files,omitempty"`

	// StartedAt is when the controller first observed the Job for the current
	// generation.
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`

	// CompletedAt is when the current generation reached Ready or Failed.
	// +optional
	CompletedAt *metav1.Time `json:"completedAt,omitempty"`

	// FailureReason is a short machine-readable failure code.
	// +optional
	FailureReason string `json:"failureReason,omitempty"`

	// FailureMessage is a human-readable failure detail.
	// +optional
	FailureMessage string `json:"failureMessage,omitempty"`

	// Conditions holds standard k8s conditions; Ready summarizes phase.
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=snap
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Observed Gen",type=integer,JSONPath=`.status.observedGeneration`
// +kubebuilder:printcolumn:name="SHA256",type=string,JSONPath=`.status.sha256`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

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
