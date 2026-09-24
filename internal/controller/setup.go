package controller

import (
	"context"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	configv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
	"github.com/example/config-distributor/internal/failinject"
)

// SetupWithManager wires the controller watches.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	mapToAll := func(ctx context.Context, _ client.Object) []reconcile.Request {
		return r.allSnapshots(ctx)
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&configv1alpha1.ConfigSnapshot{}).
		// Namespace creation / label change / deletion re-evaluates every
		// ConfigSnapshot (selector membership can change without the CS moving).
		Watches(&corev1.Namespace{}, handler.EnqueueRequestsFromMapFunc(mapToAll)).
		// Child changes (external deletion/edits) trigger the owning snapshot.
		Owns(&corev1.ConfigMap{}).
		Complete(r)
}

func (r *Reconciler) allSnapshots(ctx context.Context) []reconcile.Request {
	var list configv1alpha1.ConfigSnapshotList
	if err := r.List(ctx, &list); err != nil {
		log.FromContext(ctx).Error(err, "list ConfigSnapshots for mapping")
		return nil
	}
	reqs := make([]reconcile.Request, 0, len(list.Items))
	for _, cs := range list.Items {
		reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Name: cs.Name}})
	}
	return reqs
}

// PolicySyncer keeps the webhook matcher's FailPolicy cache in sync from the
// manager cache. It is registered as a Runnable so the webhook denies exactly
// the policies currently present in the cluster.
type PolicySyncer struct {
	Client  client.Client
	Scheme  *runtime.Scheme
	Matcher *failinject.Matcher
}

// NeedLeaderElection implements manager.LeaderElectionRunnable: the matcher
// cache must be populated even when leader election is enabled.
func (s *PolicySyncer) NeedLeaderElection() bool { return false }

// Start implements manager.Runnable.
func (s *PolicySyncer) Start(ctx context.Context) error {
	sync := func() bool {
		var list failinject.FailPolicyList
		if err := s.Client.List(ctx, &list); err != nil {
			log.FromContext(ctx).Error(err, "list FailPolicies")
			return false
		}
		s.Matcher.ReplacePolicies(list.Items)
		var nss corev1.NamespaceList
		if err := s.Client.List(ctx, &nss); err != nil {
			log.FromContext(ctx).Error(err, "list namespaces for matcher")
			return false
		}
		s.Matcher.ReplaceNamespaces(nss.Items)
		return true
	}
	// Initial load before the webhook can serve traffic.
	if !sync() {
		// Empty policy set is the safe default; periodic sync below repairs it.
		s.Matcher.ReplacePolicies(nil)
	}

	// Lightweight polling instead of a second informer: policy changes are a
	// test-only control plane and human-scale. 2s is well within demo timing.
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			sync()
		}
	}
}
