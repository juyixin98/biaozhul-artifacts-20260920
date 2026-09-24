// Package v1alpha1 is the legacy API version of the migration.example.io
// custom resources.
//
// The v1alpha1 API expresses timeouts as a plain integer number of whole
// seconds (TimeoutSeconds). It predates structured durations and the newer
// priority/tags metadata. Because v1alpha1 remains a *served* version after the
// v1 rollout, anything that only exists in v1 must survive a round-trip
// through v1alpha1 — see docs/roundtrip-strategy.md.
//
// +kubebuilder:object:generate=true
// +groupName=migration.example.io
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// GroupName is the API group shared by both served versions.
const GroupName = "migration.example.io"

// SchemeGroupVersion is the group/version tuple used when registering types.
var SchemeGroupVersion = schema.GroupVersion{Group: GroupName, Version: "v1alpha1"}

var (
	// SchemeBuilder collects the type registration functions for this package.
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	// AddToScheme installs v1alpha1 types into a runtime.Scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)

// Resource takes an unqualified resource and returns a Group qualified
// GroupResource.
func Resource(resource string) schema.GroupResource {
	return SchemeGroupVersion.WithResource(resource).GroupResource()
}

func addKnownTypes(scheme *runtime.Scheme) error {
	scheme.AddKnownTypes(SchemeGroupVersion,
		&Task{},
		&TaskList{},
	)
	metav1.AddToGroupVersion(scheme, SchemeGroupVersion)
	return nil
}

// TaskSpec is the legacy spec. Timeouts are whole seconds only; there is no
// sub-second precision and no priority/tags metadata.
type TaskSpec struct {
	// TimeoutSeconds is the operation timeout expressed as an integer number of
	// seconds. Zero means "unset" (omitted in JSON); the defaulting webhook
	// turns it into the documented default of 30 seconds.
	// +optional
	TimeoutSeconds *int64 `json:"timeoutSeconds,omitempty"`

	// Payload is an opaque command/descriptor the task executes. It is version
	// independent and copied verbatim between v1alpha1 and v1.
	// +optional
	Payload string `json:"payload,omitempty"`
}

// TaskStatus carries the last observed execution state. It is identical in
// both versions and is copied verbatim.
type TaskStatus struct {
	// Phase is the current lifecycle phase: Pending, Running, Succeeded, Failed.
	// +optional
	Phase string `json:"phase,omitempty"`

	// Retries is the number of execution attempts performed so far.
	// +optional
	Retries int32 `json:"retries,omitempty"`

	// Message is a free-form human readable status message.
	// +optional
	Message string `json:"message,omitempty"`
}

// +kubebuilder:object:root=true

// Task is the Schema for the tasks API, legacy v1alpha1 shape.
type Task struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TaskSpec   `json:"spec,omitempty"`
	Status TaskStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TaskList contains a list of Task.
type TaskList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Task `json:"items"`
}

// DeepCopyInto copies the receiver into out.
func (in *Task) DeepCopyInto(out *Task) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
	out.Status = in.Status
}

// DeepCopy returns a new deep copy of the receiver.
func (in *Task) DeepCopy() *Task {
	if in == nil {
		return nil
	}
	out := new(Task)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject implements runtime.Object.
func (in *Task) DeepCopyObject() runtime.Object {
	if c := in.DeepCopy(); c != nil {
		return c
	}
	return nil
}

// DeepCopyInto copies the receiver into out.
func (in *TaskList) DeepCopyInto(out *TaskList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		in, out := &in.Items, &out.Items
		*out = make([]Task, len(*in))
		for i := range *in {
			(*in)[i].DeepCopyInto(&(*out)[i])
		}
	}
}

// DeepCopy returns a new deep copy of the receiver.
func (in *TaskList) DeepCopy() *TaskList {
	if in == nil {
		return nil
	}
	out := new(TaskList)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject implements runtime.Object.
func (in *TaskList) DeepCopyObject() runtime.Object {
	if c := in.DeepCopy(); c != nil {
		return c
	}
	return nil
}

// DeepCopyInto copies the receiver into out.
func (in *TaskSpec) DeepCopyInto(out *TaskSpec) {
	*out = *in
	if in.TimeoutSeconds != nil {
		out.TimeoutSeconds = new(int64)
		*out.TimeoutSeconds = *in.TimeoutSeconds
	}
}

// DeepCopy returns a new deep copy of the receiver.
func (in *TaskSpec) DeepCopy() *TaskSpec {
	if in == nil {
		return nil
	}
	out := new(TaskSpec)
	in.DeepCopyInto(out)
	return out
}
