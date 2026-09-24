package controller

import (
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
)

// snapshotLabelledPredicate admits only ConfigMaps created by this controller
// (the worker result ConfigMaps), so unrelated ConfigMap churn never queues
// reconciles.
func snapshotLabelledPredicate() predicate.Predicate {
	match := func(obj client.Object) bool {
		l := obj.GetLabels()
		return l[labelPartOf] == partOfValue &&
			l[labelManagedBy] == managedBy &&
			l[labelSnapshot] != ""
	}
	return predicate.Funcs{
		CreateFunc:  func(e event.CreateEvent) bool { return match(e.Object) },
		UpdateFunc:  func(e event.UpdateEvent) bool { return match(e.ObjectNew) },
		DeleteFunc:  func(e event.DeleteEvent) bool { return match(e.Object) },
		GenericFunc: func(e event.GenericEvent) bool { return match(e.Object) },
	}
}
