// Package v1alpha1 is the spoke (legacy) version of the Timer CRD.
//
// Time fields are expressed as whole seconds. The v1 API replaced these
// with a structured Duration ({seconds, nanos}); the conversion webhook
// (see internal/conversion) owns the mapping.
package v1alpha1

import (
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// GroupVersion is group version used to register these objects.
var GroupVersion = schema.GroupVersion{Group: "timer.example.com", Version: "v1alpha1"}

// SchemeGroupVersion is an alias kept for code that expects the k8s style.
var SchemeGroupVersion = GroupVersion

// SchemeBuilder collects AddToScheme calls; Scheme ties them together.
var (
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	AddToScheme   = SchemeBuilder.AddToScheme
)

func addKnownTypes(scheme *runtime.Scheme) error {
	scheme.AddKnownTypes(GroupVersion, &Timer{}, &TimerList{})
	metav1.AddToGroupVersion(scheme, GroupVersion)
	return nil
}

// TimerSpec is the legacy shape: every duration is an integer number of
// seconds. Sub-second precision cannot be expressed in this version and the
// conversion webhook rejects it on the v1 -> v1alpha1 path rather than
// silently truncating it.
type TimerSpec struct {
	// IntervalSeconds is the timer tick interval. Required, >= 1.
	// +kubebuilder:validation:Minimum=1
	IntervalSeconds int64 `json:"intervalSeconds"`

	// TimeoutSeconds is the per-tick timeout. Optional; when omitted the
	// defaulting webhook sets 0 (meaning "no timeout").
	// +optional
	// +kubebuilder:validation:Minimum=0
	TimeoutSeconds *int64 `json:"timeoutSeconds,omitempty"`

	// Extra is a free-form bag. The CRD schema explicitly enables
	// x-kubernetes-preserve-unknown-fields for this node, so arbitrary
	// (including unknown) keys round-trip through both versions.
	// Top-level unknown fields are pruned (structural schema).
	// +optional
	Extra map[string]apiextensionsv1.JSON `json:"extra,omitempty"`
}

// TimerStatus has no observed-state semantics; it exists to prove that
// out-of-band status writes survive version conversion.
type TimerStatus struct {
	// LastFired is the last observed fire time, written by an external
	// controller that is unaware of the v1 API.
	// +optional
	LastFired *metav1.Time `json:"lastFired,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:storageversion:false

// Timer is the Schema for the timers API (v1alpha1).
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
	if in.TimeoutSeconds != nil {
		v := *in.TimeoutSeconds
		out.TimeoutSeconds = &v
	}
	if in.Extra != nil {
		out.Extra = make(map[string]apiextensionsv1.JSON, len(in.Extra))
		for k, v := range in.Extra {
			out.Extra[k] = *v.DeepCopy()
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
