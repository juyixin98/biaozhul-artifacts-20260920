// Package v1 is the storage (hub) version of the Timer CRD.
//
// It replaces the v1alpha1 integer-second fields with a structured Duration
// and adds the Priority field. Priority did not exist in v1alpha1; the
// conversion webhook carries it in an annotation
// (timer.example.com/v1-priority) so that a read/update cycle performed by
// an old v1alpha1-only client cannot erase it.
package v1

import (
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// GroupVersion is group version used to register these objects.
var GroupVersion = schema.GroupVersion{Group: "timer.example.com", Version: "v1"}

// SchemeGroupVersion is an alias kept for code that expects the k8s style.
var SchemeGroupVersion = GroupVersion

var (
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	AddToScheme   = SchemeBuilder.AddToScheme
)

func addKnownTypes(scheme *runtime.Scheme) error {
	scheme.AddKnownTypes(GroupVersion, &Timer{}, &TimerList{})
	metav1.AddToGroupVersion(scheme, GroupVersion)
	return nil
}

// Duration mirrors google.protobuf.Duration: a signed number of whole
// seconds plus a nanosecond offset carried by the same sign as seconds.
// The CRD schema enforces -999_999_999 <= nanos <= 999_999_999 and the
// validating webhook enforces the sign-consistency rule.
type Duration struct {
	// Seconds is the whole-second part. Required.
	Seconds int64 `json:"seconds"`
	// Nanos is the sub-second part, |nanos| < 1e9, same sign as seconds
	// (zero when seconds is zero or the value is whole seconds).
	// +optional
	Nanos int32 `json:"nanos,omitempty"`
}

// TimerSpec is the v1 shape.
type TimerSpec struct {
	// Interval is the timer tick interval, e.g. {"seconds": 5, "nanos": 500000000}.
	Interval Duration `json:"interval"`
	// Timeout is the per-tick timeout; zero duration means "no timeout".
	// +optional
	Timeout *Duration `json:"timeout,omitempty"`
	// Priority is new in v1. Values: Normal | Low | High. Defaults to
	// Normal via the mutating webhook. It round-trips to v1alpha1 clients
	// through the v1-priority annotation.
	// +optional
	// +kubebuilder:validation:Enum=Low;Normal;High
	Priority string `json:"priority,omitempty"`
	// Extra is a free-form bag preserved across conversion
	// (x-kubernetes-preserve-unknown-fields in the CRD schema).
	// +optional
	Extra map[string]apiextensionsv1.JSON `json:"extra,omitempty"`
}

// TimerStatus mirrors v1alpha1; it travels through conversion verbatim.
type TimerStatus struct {
	// LastFired is the last observed fire time.
	// +optional
	LastFired *metav1.Time `json:"lastFired,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:storageversion

// Timer is the Schema for the timers API (v1, storage version).
type Timer struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   TimerSpec   `json:"spec,omitempty"`
	Status TimerStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// TimerList contains a list of Timer.
type TimerList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Timer `json:"items"`
}

// DeepCopyInto copies the receiver into out.
func (in *Timer) DeepCopyInto(out *Timer) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
	in.Status.DeepCopyInto(&out.Status)
}

// DeepCopy creates a new Timer.
func (in *Timer) DeepCopy() *Timer {
	if in == nil {
		return nil
	}
	out := new(Timer)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject returns runtime.Object.
func (in *Timer) DeepCopyObject() runtime.Object {
	return in.DeepCopy()
}

// DeepCopyInto copies the receiver into out.
func (in *TimerList) DeepCopyInto(out *TimerList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		l := make([]Timer, len(in.Items))
		for i := range in.Items {
			in.Items[i].DeepCopyInto(&l[i])
		}
		out.Items = l
	}
}

// DeepCopy creates a new TimerList.
func (in *TimerList) DeepCopy() *TimerList {
	if in == nil {
		return nil
	}
	out := new(TimerList)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject returns runtime.Object.
func (in *TimerList) DeepCopyObject() runtime.Object {
	return in.DeepCopy()
}

// DeepCopyInto copies the receiver into out.
func (in *TimerSpec) DeepCopyInto(out *TimerSpec) {
	*out = *in
	if in.Timeout != nil {
		t := *in.Timeout
		out.Timeout = &t
	}
	if in.Extra != nil {
		out.Extra = make(map[string]apiextensionsv1.JSON, len(in.Extra))
		for k, val := range in.Extra {
			out.Extra[k] = *val.DeepCopy()
		}
	}
}

// DeepCopy creates a new TimerSpec.
func (in *TimerSpec) DeepCopy() *TimerSpec {
	if in == nil {
		return nil
	}
	out := new(TimerSpec)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *TimerStatus) DeepCopyInto(out *TimerStatus) {
	*out = *in
	if in.LastFired != nil {
		out.LastFired = in.LastFired.DeepCopy()
	}
}

// DeepCopy creates a new TimerStatus.
func (in *TimerStatus) DeepCopy() *TimerStatus {
	if in == nil {
		return nil
	}
	out := new(TimerStatus)
	in.DeepCopyInto(out)
	return out
}
