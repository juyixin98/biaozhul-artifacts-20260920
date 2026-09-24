// Package failinject implements an optional chaos-injection mechanism: a
// FailPolicy object makes the in-cluster admission webhook deny matching
// ConfigMap write operations N times. This gives the controller deterministic,
// repeatable "partial failure" scenarios without ever killing the controller
// or forging fake errors in the reconciler.
package failinject

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// GroupName is the API group for FailPolicy.
const GroupName = "chaos.example.com"

// SchemeGroupVersion is the group version used to register FailPolicy.
var SchemeGroupVersion = schema.GroupVersion{Group: GroupName, Version: "v1alpha1"}

var (
	// SchemeBuilder registers the fail-injection types.
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)
	// AddToScheme adds the fail-injection types to a scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)

func addKnownTypes(scheme *runtime.Scheme) error {
	scheme.AddKnownTypes(SchemeGroupVersion,
		&FailPolicy{},
		&FailPolicyList{},
	)
	metav1.AddToGroupVersion(scheme, SchemeGroupVersion)
	return nil
}

const (
	// ActionCreate denies CREATE requests.
	ActionCreate = "CREATE"
	// ActionUpdate denies UPDATE requests.
	ActionUpdate = "UPDATE"
	// ActionDelete denies DELETE requests.
	ActionDelete = "DELETE"
)

const (
	// ModeAlways denies every matching request (until the policy is deleted).
	ModeAlways = "Always"
	// ModeFirstN denies only the first FirstN matching requests, counted in the
	// webhook process. The counter survives resyncs but resets on restart, like
	// a real flaky dependency recovering.
	ModeFirstN = "FirstN"
)

// FailPolicySpec describes which requests to deny and how often.
type FailPolicySpec struct {
	// TargetNamespaces limits the policy to these namespace names. Empty means
	// every namespace.
	// +optional
	TargetNamespaces []string `json:"targetNamespaces,omitempty"`
	// NamespaceSelector limits the policy to namespaces carrying ALL these
	// labels. Empty matches everything.
	// +optional
	NamespaceSelector map[string]string `json:"namespaceSelector,omitempty"`
	// Actions are the admission operations denied: CREATE, UPDATE, DELETE.
	// Empty defaults to CREATE only.
	// +optional
	Actions []string `json:"actions,omitempty"`
	// ObjectNameSubstring, when set, only matches objects whose name contains
	// this string.
	// +optional
	ObjectNameSubstring string `json:"objectNameSubstring,omitempty"`
	// Mode is Always or FirstN.
	// +kubebuilder:validation:Enum=Always;FirstN
	Mode string `json:"mode"`
	// FirstN is the number of matching requests denied in FirstN mode.
	// +optional
	FirstN int32 `json:"firstN,omitempty"`
	// Message is the denial message shown in the API server response.
	// +optional
	Message string `json:"message,omitempty"`
}

// +kubebuilder:object:root=true

// FailPolicy makes the chaos admission webhook deny matching ConfigMap
// requests.
type FailPolicy struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec FailPolicySpec `json:"spec,omitempty"`
}

// +kubebuilder:object:root=true

// FailPolicyList contains a list of FailPolicy.
type FailPolicyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []FailPolicy `json:"items"`
}

// DeepCopyInto copies the receiver into out.
func (in *FailPolicy) DeepCopyInto(out *FailPolicy) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
}

// DeepCopyObject implements runtime.Object.
func (in *FailPolicy) DeepCopyObject() runtime.Object {
	if in == nil {
		return nil
	}
	out := new(FailPolicy)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *FailPolicyList) DeepCopyInto(out *FailPolicyList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		out.Items = make([]FailPolicy, len(in.Items))
		for i := range in.Items {
			in.Items[i].DeepCopyInto(&out.Items[i])
		}
	}
}

// DeepCopyObject implements runtime.Object.
func (in *FailPolicyList) DeepCopyObject() runtime.Object {
	if in == nil {
		return nil
	}
	out := new(FailPolicyList)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *FailPolicySpec) DeepCopyInto(out *FailPolicySpec) {
	*out = *in
	if in.TargetNamespaces != nil {
		out.TargetNamespaces = append([]string(nil), in.TargetNamespaces...)
	}
	if in.NamespaceSelector != nil {
		out.NamespaceSelector = make(map[string]string, len(in.NamespaceSelector))
		for k, v := range in.NamespaceSelector {
			out.NamespaceSelector[k] = v
		}
	}
	if in.Actions != nil {
		out.Actions = append([]string(nil), in.Actions...)
	}
}
