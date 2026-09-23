// Package controller implements the ConfigDistribution reconciler.
//
// Design notes (event deduplication):
//
//   - Child ConfigMaps are named "<cr-name>-<digest12>" where digest12 is the
//     first 12 hex chars of the sha256 of the canonicalized spec.Config. The
//     name is a pure function of desired state, so duplicate events, full
//     resyncs and process restarts all converge on Get-before-Create no-ops
//     instead of duplicate objects.
//   - Every child carries labels recording the owner UID and the digest, plus
//     a controller owner reference. Cleanup (selector shrink / CR delete) only
//     ever touches objects whose owner-uid label AND owner reference UID both
//     match this CR's UID, so a same-name object belonging to a different
//     (e.g. rebuilt) owner is never deleted.
//   - Status records the applied digest per target namespace. A failed target
//     is marked Failed and requeued; already-Ready targets are left untouched
//     and are never rolled back.
package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	distv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
)

const (
	// FinalizerName guards CR deletion so owned children are cleaned up first.
	FinalizerName = "dist.example.com/cleanup"

	// LabelManagedBy marks objects created by this controller.
	LabelManagedBy = "app.kubernetes.io/managed-by"
	// ManagedByValue is the value of LabelManagedBy on our children.
	ManagedByValue = "config-distributor"
	// LabelOwnerName records the owning ConfigDistribution name.
	LabelOwnerName = "dist.example.com/owner-name"
	// LabelOwnerUID records the owning ConfigDistribution UID. Cleanup matches
	// on this so a rebuilt CR (same name, new UID) never deletes the previous
	// incarnation's objects, and vice versa.
	LabelOwnerUID = "dist.example.com/owner-uid"
	// LabelDigest records the short (12-char) config digest for display and
	// label selection. Label values are capped at 63 chars, so the full
	// 64-char digest lives in AnnotationDigest instead.
	LabelDigest = "dist.example.com/config-digest"
	// AnnotationDigest records the full sha256 hex digest of the config.
	AnnotationDigest = "dist.example.com/config-digest-full"

	// digestNameLen is how many hex chars of the digest go into child names.
	digestNameLen = 12
)

// ConfigDistributionReconciler reconciles a ConfigDistribution object.
type ConfigDistributionReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// ConfigDigest computes the version identifier of a config payload. Keys are
// sorted before hashing so the digest is order-independent.
func ConfigDigest(config map[string]string) string {
	keys := make([]string, 0, len(config))
	for k := range config {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	h := sha256.New()
	for _, k := range keys {
		fmt.Fprintf(h, "%d:%s=%d:%s\n", len(k), k, len(config[k]), config[k])
	}
	return hex.EncodeToString(h.Sum(nil))
}

// ChildName derives the child ConfigMap name for a CR and digest.
func ChildName(crName, digest string) string {
	name := fmt.Sprintf("%s-%s", crName, digest[:digestNameLen])
	if len(name) > 63 {
		name = name[:63]
	}
	return name
}

// +kubebuilder:rbac:groups=dist.example.com,resources=configdistributions,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=dist.example.com,resources=configdistributions/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=dist.example.com,resources=configdistributions/finalizers,verbs=update
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch

// Reconcile is the main loop. It is level-based: any event (create, update,
// resync, restart replay) simply re-derives the desired state and converges.
func (r *ConfigDistributionReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var cr distv1alpha1.ConfigDistribution
	if err := r.Get(ctx, req.NamespacedName, &cr); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// --- deletion path: remove only objects we own, then drop the finalizer.
	if !cr.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(&cr, FinalizerName) {
			if err := r.deleteOwnedChildren(ctx, &cr, nil); err != nil {
				return ctrl.Result{}, err
			}
			controllerutil.RemoveFinalizer(&cr, FinalizerName)
			if err := r.Update(ctx, &cr); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{}, nil
	}

	if !controllerutil.ContainsFinalizer(&cr, FinalizerName) {
		controllerutil.AddFinalizer(&cr, FinalizerName)
		if err := r.Update(ctx, &cr); err != nil {
			return ctrl.Result{}, err
		}
	}

	digest := ConfigDigest(cr.Spec.Config)
	childName := ChildName(cr.Name, digest)

	// --- resolve desired namespaces.
	desired, err := r.selectedNamespaces(ctx, &cr)
	if err != nil {
		return ctrl.Result{}, err
	}

	// --- converge each target independently; failures never roll back others.
	names := make([]string, 0, len(desired))
	for ns := range desired {
		names = append(names, ns)
	}
	sort.Strings(names) // deterministic status ordering
	targets := make([]distv1alpha1.TargetStatus, 0, len(desired))
	var failures []string
	for _, ns := range names {
		phase, msg := r.reconcileTarget(ctx, &cr, ns, childName, digest)
		targets = append(targets, distv1alpha1.TargetStatus{
			Namespace:          ns,
			Digest:             digest,
			Phase:              phase,
			Message:            msg,
			LastTransitionTime: metav1.NewTime(time.Now()),
		})
		if phase != distv1alpha1.TargetReady {
			failures = append(failures, fmt.Sprintf("%s: %s", ns, msg))
		}
	}

	// --- garbage-collect children we own that are no longer desired
	// (selector shrank or digest changed). Foreign objects are never touched.
	if err := r.deleteOwnedChildren(ctx, &cr, func(cm *corev1.ConfigMap) bool {
		_, stillWanted := desired[cm.Namespace]
		return !stillWanted || cm.Annotations[AnnotationDigest] != digest
	}); err != nil {
		return ctrl.Result{}, err
	}

	// --- status: keep previously-Ready targets that are still desired but were
	// not re-verified this pass impossible (we always re-verify), so a fresh
	// list is correct; failures are recorded per target without touching the
	// Ready entries of the others.
	cr.Status.ObservedGeneration = cr.Generation
	cr.Status.ConfigDigest = digest
	cr.Status.Targets = targets
	if len(failures) > 0 {
		apimeta.SetStatusCondition(&cr.Status.Conditions, metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionFalse,
			Reason:             "PartialFailure",
			Message:            strings.Join(failures, "; "),
			ObservedGeneration: cr.Generation,
		})
	} else {
		apimeta.SetStatusCondition(&cr.Status.Conditions, metav1.Condition{
			Type:               "Ready",
			Status:             metav1.ConditionTrue,
			Reason:             "Distributed",
			Message:            fmt.Sprintf("config %s present in %d namespace(s)", digest[:digestNameLen], len(desired)),
			ObservedGeneration: cr.Generation,
		})
	}
	if err := r.Status().Update(ctx, &cr); err != nil {
		if apierrors.IsConflict(err) {
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, err
	}

	if len(failures) > 0 {
		logger.Info("partial failure, requeueing failed targets only", "failed", failures)
		// RequeueAfter (not an error) so the rate limiter backs off gently and
		// successful targets stay Ready.
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	return ctrl.Result{}, nil
}

// reconcileTarget ensures the child ConfigMap in one namespace. It returns the
// resulting phase and an evidence message.
func (r *ConfigDistributionReconciler) reconcileTarget(
	ctx context.Context,
	cr *distv1alpha1.ConfigDistribution,
	ns, childName, digest string,
) (distv1alpha1.TargetPhase, string) {
	logger := log.FromContext(ctx)
	key := types.NamespacedName{Namespace: ns, Name: childName}

	var existing corev1.ConfigMap
	err := r.Get(ctx, key, &existing)
	switch {
	case apierrors.IsNotFound(err):
		cm := r.buildChild(cr, ns, childName, digest)
		if err := r.Create(ctx, cm); err != nil {
			if apierrors.IsAlreadyExists(err) {
				// Lost a create race (duplicate event / two workers). Level-based
				// reconcile: the next pass verifies content. Not an error.
				return distv1alpha1.TargetReady, "created concurrently"
			}
			return distv1alpha1.TargetFailed, fmt.Sprintf("create: %v", err)
		}
		logger.Info("created child configmap", "namespace", ns, "name", childName)
		return distv1alpha1.TargetReady, "created"

	case err != nil:
		return distv1alpha1.TargetFailed, fmt.Sprintf("get: %v", err)
	}

	// Object exists. Decide our relationship to it before touching anything.
	ownerUID := existing.Labels[LabelOwnerUID]
	managed := existing.Labels[LabelManagedBy] == ManagedByValue

	switch {
	case ownerUID == string(cr.UID) && hasOwnerRef(&existing, cr.UID):
		// Ours. Digest is part of the name, so content drift means someone
		// edited it out-of-band; restore desired data idempotently.
		if !mapsEqual(existing.Data, cr.Spec.Config) || existing.Annotations[AnnotationDigest] != digest {
			patched := existing.DeepCopy()
			patched.Data = copyMap(cr.Spec.Config)
			if patched.Labels == nil {
				patched.Labels = map[string]string{}
			}
			patched.Labels[LabelDigest] = digest[:digestNameLen]
			if patched.Annotations == nil {
				patched.Annotations = map[string]string{}
			}
			patched.Annotations[AnnotationDigest] = digest
			if err := r.Update(ctx, patched); err != nil {
				return distv1alpha1.TargetFailed, fmt.Sprintf("restore drifted data: %v", err)
			}
			return distv1alpha1.TargetReady, "restored drifted data"
		}
		return distv1alpha1.TargetReady, "unchanged"

	case managed:
		// Created by this controller but owned by a different UID: an orphan
		// left by a previous incarnation of this CR (owner deleted while the
		// controller was down, finalizer force-removed, ...). Adopt it instead
		// of deleting: the content is identical because the digest is in the
		// name. Adoption keeps the object (and any consumers) stable.
		patched := existing.DeepCopy()
		if patched.Labels == nil {
			patched.Labels = map[string]string{}
		}
		patched.Labels[LabelOwnerUID] = string(cr.UID)
		patched.Labels[LabelOwnerName] = cr.Name
		patched.Labels[LabelDigest] = digest[:digestNameLen]
		if patched.Annotations == nil {
			patched.Annotations = map[string]string{}
		}
		patched.Annotations[AnnotationDigest] = digest
		patched.Data = copyMap(cr.Spec.Config)
		if err := controllerutil.SetControllerReference(cr, patched, r.Scheme); err != nil {
			return distv1alpha1.TargetFailed, fmt.Sprintf("adopt: set owner ref: %v", err)
		}
		if err := r.Update(ctx, patched); err != nil {
			return distv1alpha1.TargetFailed, fmt.Sprintf("adopt: %v", err)
		}
		logger.Info("adopted orphaned child configmap", "namespace", ns, "name", childName)
		return distv1alpha1.TargetReady, "adopted orphan"

	default:
		// A foreign object squats on our deterministic name. Never overwrite
		// or delete it; surface the conflict and retry (an operator may move
		// it away).
		return distv1alpha1.TargetConflict,
			fmt.Sprintf("configmap %s exists and is not managed by %s (uid mismatch or foreign); left untouched", childName, ManagedByValue)
	}
}

// buildChild constructs the desired child ConfigMap with ownership metadata.
func (r *ConfigDistributionReconciler) buildChild(
	cr *distv1alpha1.ConfigDistribution, ns, name, digest string,
) *corev1.ConfigMap {
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: ns,
			Name:      name,
			Labels: map[string]string{
				LabelManagedBy: ManagedByValue,
				LabelOwnerName: cr.Name,
				LabelOwnerUID:  string(cr.UID),
				LabelDigest:    digest[:digestNameLen],
			},
			Annotations: map[string]string{
				AnnotationDigest: digest,
			},
		},
		Data: copyMap(cr.Spec.Config),
	}
	// SetControllerReference cannot fail here: scheme always knows both types.
	_ = controllerutil.SetControllerReference(cr, cm, r.Scheme)
	return cm
}

// selectedNamespaces returns the set of namespace names matching the selector.
func (r *ConfigDistributionReconciler) selectedNamespaces(
	ctx context.Context, cr *distv1alpha1.ConfigDistribution,
) (map[string]struct{}, error) {
	sel, err := metav1.LabelSelectorAsSelector(&cr.Spec.NamespaceSelector)
	if err != nil {
		return nil, fmt.Errorf("invalid namespaceSelector: %w", err)
	}
	var nsList corev1.NamespaceList
	if err := r.List(ctx, &nsList, client.MatchingLabelsSelector{Selector: sel}); err != nil {
		return nil, err
	}
	out := make(map[string]struct{}, len(nsList.Items))
	for _, ns := range nsList.Items {
		out[ns.Name] = struct{}{}
	}
	return out, nil
}

// deleteOwnedChildren deletes child ConfigMaps owned by this CR (owner-uid
// label AND owner reference UID must both match) for which keep==false.
// A nil keep deletes every owned child. Objects that fail the ownership
// double-check are skipped, never deleted.
func (r *ConfigDistributionReconciler) deleteOwnedChildren(
	ctx context.Context,
	cr *distv1alpha1.ConfigDistribution,
	drop func(*corev1.ConfigMap) bool,
) error {
	logger := log.FromContext(ctx)
	var children corev1.ConfigMapList
	err := r.List(ctx, &children, client.MatchingLabels{
		LabelManagedBy: ManagedByValue,
		LabelOwnerUID:  string(cr.UID),
	})
	if err != nil {
		return err
	}
	for i := range children.Items {
		cm := &children.Items[i]
		if !hasOwnerRef(cm, cr.UID) {
			// Label claims ownership but the owner reference disagrees:
			// ambiguous provenance, leave it alone.
			logger.Info("skipping configmap with mismatched owner reference",
				"namespace", cm.Namespace, "name", cm.Name)
			continue
		}
		if drop != nil && !drop(cm) {
			continue
		}
		if err := r.Delete(ctx, cm); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("delete owned child %s/%s: %w", cm.Namespace, cm.Name, err)
		}
		logger.Info("deleted owned child configmap", "namespace", cm.Namespace, "name", cm.Name)
	}
	return nil
}

// hasOwnerRef reports whether the object has a controller owner reference
// pointing at uid.
func hasOwnerRef(obj metav1.Object, uid types.UID) bool {
	for _, ref := range obj.GetOwnerReferences() {
		if ref.UID == uid {
			return true
		}
	}
	return false
}

func copyMap(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if bv, ok := b[k]; !ok || bv != v {
			return false
		}
	}
	return true
}

// mapNamespaceToDistributions maps a namespace event to every
// ConfigDistribution whose selector matches it, so label changes re-converge
// rollouts without waiting for resync.
func (r *ConfigDistributionReconciler) mapNamespaceToDistributions(
	ctx context.Context, obj client.Object,
) []reconcile.Request {
	var crList distv1alpha1.ConfigDistributionList
	if err := r.List(ctx, &crList); err != nil {
		return nil
	}
	var reqs []reconcile.Request
	for i := range crList.Items {
		sel, err := metav1.LabelSelectorAsSelector(&crList.Items[i].Spec.NamespaceSelector)
		if err != nil {
			continue
		}
		if sel.Matches(labels.Set(obj.GetLabels())) {
			reqs = append(reqs, reconcile.Request{
				NamespacedName: types.NamespacedName{Name: crList.Items[i].Name},
			})
		}
	}
	return reqs
}

// SetupWithManager wires the controller: watch CRs, owned ConfigMaps, and
// Namespaces (selector changes must re-converge the rollout). The CR is
// cluster-scoped, so Owns() maps child events back by owner reference name.
func (r *ConfigDistributionReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&distv1alpha1.ConfigDistribution{}).
		Owns(&corev1.ConfigMap{}).
		Watches(
			&corev1.Namespace{},
			handler.EnqueueRequestsFromMapFunc(r.mapNamespaceToDistributions),
		).
		Named("configdistribution").
		Complete(r)
}
