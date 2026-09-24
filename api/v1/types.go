// Package v1 is the current storage API version of the migration.example.io
// custom resources.
//
// Compared with v1alpha1 it replaces the loose "integer seconds" timeout with
// a structured, protobuf-style Duration (seconds + non-negative nanosecond
// offset) and adds scheduling metadata (Priority, Tags). The conversion
// webhook maps the old integer field onto the structured duration; sub-second
// precision and the new metadata have no v1alpha1 representation, so they are
// stashed in a reserved annotation for the duration that an old client keeps
// the object on the cluster (see docs/roundtrip-strategy.md).
//
// +kubebuilder:object:generate=true
// +groupName=migration.example.io
package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// GroupName is the API group shared by both served versions.
const GroupName = "migration.example.io"

// SchemeGroupVersion is the group/version tuple used when registering types.
var SchemeGroupVersion = schema.GroupVersion{Group: GroupName, Version: "v1"}

var (
	// SchemeBuilder collects the type registration functions for this package.
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	// AddToScheme installs v1 types into a runtime.Scheme.
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

// Duration bounds mirror google.protobuf.Duration: Seconds is the signed whole
// number of seconds and Nanos is the offset within the second, expressed in
// nanoseconds. Nanos must be in [0, 999_999_999] when Seconds >= 0 (we do not
// allow negative timeouts at all, validated by the webhook). Keeping nanos in
// the same sign as seconds is exactly what prevents values such as
// "1 second minus one nanosecond" being silently written off as 1s.
const (
	// MaxDurationNanos is the exclusive upper bound of the nanos field.
	MaxDurationNanos int64 = 1_000_000_000
	// MaxDurationSeconds is an arbitrary but finite upper bound (~10000 years)
	// used by validation to reject obviously broken input instead of silently
	// accepting int64 overflow.
	MaxDurationSeconds int64 = 315_576_000_000
)

// Duration is the structured replacement for v1alpha1.TimeoutSeconds.
type Duration struct {
	// Seconds is the whole-seconds part of the duration. Must be >= 0 and no
	// greater than MaxDurationSeconds.
	Seconds int64 `json:"seconds"`

	// Nanos is the sub-second part, in nanoseconds, range [0, 999999999].
	// It must be zero when the value needs to round-trip through v1alpha1,
	// which cannot express sub-second precision: the conversion webhook
	// rejects such values with an explicit error rather than truncating.
	// +optional
	Nanos int32 `json:"nanos,omitempty"`
}

// TaskPriority enumerates scheduling classes.
// +kubebuilder:validation:Enum=Normal;High;Low
type TaskPriority string

const (
	// PriorityNormal is the default scheduling class applied by the defaulting
	// webhook when priority is omitted.
	PriorityNormal TaskPriority = "Normal"
	// PriorityHigh schedules ahead of Normal tasks.
	PriorityHigh TaskPriority = "High"
	// PriorityLow yields to Normal tasks.
	PriorityLow TaskPriority = "Low"
)

// TaskSpec is the current spec shape.
type TaskSpec struct {
	// Timeout is the structured operation timeout. The defaulting webhook
	// fills it with 30s when omitted.
	// +optional
	Timeout *Duration `json:"timeout,omitempty"`

	// Payload is copied verbatim between versions.
	// +optional
	Payload string `json:"payload,omitempty"`

	// Priority is a v1-only scheduling class. Defaults to Normal.
	// +optional
	Priority TaskPriority `json:"priority,omitempty"`

	// Tags are v1-only free-form labels used by the scheduler. They have no
	// v1alpha1 field and ride through old clients inside
	// migration.example.io/v1-spec-preserve.
	// +optional
	Tags []string `json:"tags,omitempty"`
}

// TaskStatus carries the last observed execution state, identical across
// versions.
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

// Task is the Schema for the tasks API, current v1 shape.
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
func (in *Duration) DeepCopyInto(out *Duration) {
	*out = *in
}

// DeepCopy returns a new deep copy of the receiver.
func (in *Duration) DeepCopy() *Duration {
	if in == nil {
		return nil
	}
	out := new(Duration)
	in.DeepCopyInto(out)
	return out
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
	if in.Timeout != nil {
		out.Timeout = new(Duration)
		*out.Timeout = *in.Timeout
	}
	if in.Tags != nil {
		out.Tags = append([]string(nil), in.Tags...)
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
