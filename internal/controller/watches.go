package controller

import (
	"context"

	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// enqueueOwningCertificate maps a Secret carrying the owner label back to its
// Certificate. This catches external Secret edits (update conflicts, manual
// kubectl writes) immediately instead of waiting for the periodic requeue.
func enqueueOwningCertificate() handler.EventHandler {
	return handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
		name := obj.GetLabels()[LabelOwnerName]
		if name == "" {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: name}}}
	})
}

// secretHasOwnerPredicate limits Secret watch traffic to coordinator-owned
// secrets.
func secretHasOwnerPredicate() predicate.Predicate {
	return predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool { return hasOwner(e.Object) },
		UpdateFunc: func(e event.UpdateEvent) bool { return hasOwner(e.ObjectNew) || hasOwner(e.ObjectOld) },
		DeleteFunc: func(e event.DeleteEvent) bool { return hasOwner(e.Object) },
	}
}

func hasOwner(obj client.Object) bool {
	if obj == nil {
		return false
	}
	return obj.GetLabels()[LabelOwnerName] != ""
}

// enqueueByRequestAnnotation maps a CertificateRequest event back to its
// owning Certificate via the owner annotation (requests intentionally have no
// ownerReference so they remain as an audit trail after completion).
func enqueueByRequestAnnotation() handler.EventHandler {
	return handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
		name := obj.GetAnnotations()[AnnotationOwnerName]
		if name == "" {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: name}}}
	})
}
