package controller

import (
	"context"

	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
)

// isResultCM narrows the ConfigMap watch to our labeled result ConfigMaps so
// unrelated cluster ConfigMaps do not trigger reconciles.
func isResultCM(obj client.Object) bool {
	if obj == nil {
		return false
	}
	l := obj.GetLabels()
	return l != nil && l[LabelComponent] == ComponentResult
}

// resultCMMapper maps a result ConfigMap event to the Snapshot that owns it
// via ownerReferences.
var resultCMMapper = handler.EnqueueRequestsFromMapFunc(func(_ context.Context, obj client.Object) []reconcile.Request {
	if obj == nil {
		return nil
	}
	for _, ref := range obj.GetOwnerReferences() {
		if ref.Kind == "Snapshot" && ref.APIVersion == snapshotv1alpha1.GroupVersion.String() {
			return []reconcile.Request{{NamespacedName: types.NamespacedName{
				Name:      ref.Name,
				Namespace: obj.GetNamespace(),
			}}}
		}
	}
	return nil
})
