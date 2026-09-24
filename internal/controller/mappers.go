package controller

import (
	"context"

	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	quota "resourcequota-reservation/api/v1alpha1"
)

// claimEnqueueMapper maps a Pod event to the claim named in its
// quota.example.com/claim label.
func claimEnqueueMapper(_ client.Client) handler.MapFunc {
	return func(ctx context.Context, obj client.Object) []reconcile.Request {
		name := obj.GetLabels()[quota.ClaimLabelKey]
		if name == "" {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: name}}}
	}
}

// poolToClaimsMapper maps a ReservationPool status change to every
// non-terminal claim in that namespace. Used when a pool is (rarely) created
// lazily or repaired: pending claims get another chance to debit.
func poolToClaimsMapper(c client.Client) handler.MapFunc {
	return func(ctx context.Context, obj client.Object) []reconcile.Request {
		var list quota.ResourceClaimList
		if err := c.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
			return nil
		}
		out := make([]reconcile.Request, 0, len(list.Items))
		for i := range list.Items {
			ph := list.Items[i].Status.Phase
			if ph == quota.PhaseRejected || ph == quota.PhaseExpired {
				continue
			}
			out = append(out, reconcile.Request{NamespacedName: types.NamespacedName{
				Namespace: list.Items[i].Namespace,
				Name:      list.Items[i].Name,
			}})
		}
		return out
	}
}

// poolStatusChangedPredicate filters pool watch events to status updates so a
// busy pool does not hot-loop the claim queue on metadata-only changes.
type poolStatusChangedPredicate struct{}

func (poolStatusChangedPredicate) Create(event.CreateEvent) bool   { return true }
func (poolStatusChangedPredicate) Delete(event.DeleteEvent) bool   { return true }
func (poolStatusChangedPredicate) Generic(event.GenericEvent) bool { return false }
func (poolStatusChangedPredicate) Update(e event.UpdateEvent) bool {
	return e.ObjectOld.GetResourceVersion() != e.ObjectNew.GetResourceVersion()
}
