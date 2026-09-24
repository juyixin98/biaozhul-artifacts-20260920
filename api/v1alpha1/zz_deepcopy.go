// Hand-written deepcopy methods so the project builds without running
// controller-gen. They must be kept in sync with the types above.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	runtime "k8s.io/apimachinery/pkg/runtime"
)

// DeepCopyInto copies the receiver into out.
func (in *Payload) DeepCopyInto(out *Payload) {
	*out = *in
}

// DeepCopy returns a new copy of Payload.
func (in *Payload) DeepCopy() *Payload {
	if in == nil {
		return nil
	}
	out := new(Payload)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *ConfigSnapshotSpec) DeepCopyInto(out *ConfigSnapshotSpec) {
	*out = *in
	in.Payload.DeepCopyInto(&out.Payload)
	in.Selector.DeepCopyInto(&out.Selector)
}

// DeepCopy returns a new copy of ConfigSnapshotSpec.
func (in *ConfigSnapshotSpec) DeepCopy() *ConfigSnapshotSpec {
	if in == nil {
		return nil
	}
	out := new(ConfigSnapshotSpec)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *TargetStatus) DeepCopyInto(out *TargetStatus) {
	*out = *in
}

// DeepCopy returns a new copy of TargetStatus.
func (in *TargetStatus) DeepCopy() *TargetStatus {
	if in == nil {
		return nil
	}
	out := new(TargetStatus)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *ConfigSnapshotStatus) DeepCopyInto(out *ConfigSnapshotStatus) {
	*out = *in
	if in.Targets != nil {
		out.Targets = make([]TargetStatus, len(in.Targets))
		copy(out.Targets, in.Targets)
	}
	if in.Conditions != nil {
		out.Conditions = make([]metav1.Condition, len(in.Conditions))
		for i := range in.Conditions {
			in.Conditions[i].DeepCopyInto(&out.Conditions[i])
		}
	}
}

// DeepCopy returns a new copy of ConfigSnapshotStatus.
func (in *ConfigSnapshotStatus) DeepCopy() *ConfigSnapshotStatus {
	if in == nil {
		return nil
	}
	out := new(ConfigSnapshotStatus)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyInto copies the receiver into out.
func (in *ConfigSnapshot) DeepCopyInto(out *ConfigSnapshot) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ObjectMeta.DeepCopyInto(&out.ObjectMeta)
	in.Spec.DeepCopyInto(&out.Spec)
	in.Status.DeepCopyInto(&out.Status)
}

// DeepCopy returns a new copy of ConfigSnapshot.
func (in *ConfigSnapshot) DeepCopy() *ConfigSnapshot {
	if in == nil {
		return nil
	}
	out := new(ConfigSnapshot)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject implements runtime.Object.
func (in *ConfigSnapshot) DeepCopyObject() runtime.Object {
	if c := in.DeepCopy(); c != nil {
		return c
	}
	return nil
}

// DeepCopyInto copies the receiver into out.
func (in *ConfigSnapshotList) DeepCopyInto(out *ConfigSnapshotList) {
	*out = *in
	out.TypeMeta = in.TypeMeta
	in.ListMeta.DeepCopyInto(&out.ListMeta)
	if in.Items != nil {
		out.Items = make([]ConfigSnapshot, len(in.Items))
		for i := range in.Items {
			in.Items[i].DeepCopyInto(&out.Items[i])
		}
	}
}

// DeepCopy returns a new copy of ConfigSnapshotList.
func (in *ConfigSnapshotList) DeepCopy() *ConfigSnapshotList {
	if in == nil {
		return nil
	}
	out := new(ConfigSnapshotList)
	in.DeepCopyInto(out)
	return out
}

// DeepCopyObject implements runtime.Object.
func (in *ConfigSnapshotList) DeepCopyObject() runtime.Object {
	if c := in.DeepCopy(); c != nil {
		return c
	}
	return nil
}
