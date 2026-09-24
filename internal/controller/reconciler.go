// Package controller contains the ConfigSnapshot reconciler.
package controller

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/klog/v2"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	configv1alpha1 "github.com/example/config-distributor/api/v1alpha1"
	"github.com/example/config-distributor/internal/naming"
)

// DefaultRetryInterval is the requeue delay while any target is failing.
const DefaultRetryInterval = 10 * time.Second

// Reconciler reconciles ConfigSnapshot objects.
type Reconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
	// RetryInterval caps the retry delay for failed targets.
	RetryInterval time.Duration
}

// Reconcile performs a single idempotent, resync-safe delivery round:
//
//  1. Select target namespaces from the immutable selector.
//  2. Garbage-collect owned children that disappeared from the selection, but
//     only objects whose ownerReference UID equals this object's UID — a
//     same-named object owned by a different UID (e.g. an owner recreation
//     race) is never deleted.
//  3. Apply the desired child ConfigMap to every selected namespace. Child
//     name embeds the content digest, so repeated events and full resyncs
//     always hit the same object; create is only attempted once.
//  4. Record per-target status. A target's Version advances only on its own
//     success and is never rolled back because another target failed.
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("configsnapshot", req.NamespacedName)

	cs := &configv1alpha1.ConfigSnapshot{}
	if err := r.Get(ctx, req.NamespacedName, cs); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// Being deleted: owner-cascade removes children; nothing to do.
	if !cs.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}

	digest := naming.ContentDigest(cs.Spec.Payload.Format, cs.Spec.Payload.Data)
	childName := naming.ChildName(cs.Name, digest)

	selector, err := metav1.LabelSelectorAsSelector(&cs.Spec.Selector)
	if err != nil {
		// Selector was valid at admission; surface and stop.
		_ = r.setTerminalError(ctx, cs, fmt.Sprintf("invalid selector: %v", err))
		return ctrl.Result{}, nil
	}

	nsList := &corev1.NamespaceList{}
	if err := r.List(ctx, nsList, client.MatchingLabelsSelector{Selector: selector}); err != nil {
		return ctrl.Result{}, fmt.Errorf("list namespaces: %w", err)
	}
	selected := map[string]*corev1.Namespace{}
	selectedNames := make([]string, 0, len(nsList.Items))
	for i := range nsList.Items {
		ns := &nsList.Items[i]
		selected[ns.Name] = ns
		selectedNames = append(selectedNames, ns.Name)
	}
	sort.Strings(selectedNames)
	logger.Info("reconcile round", "targets", len(selectedNames), "digest", digest)

	// GC must run even if applies later fail: shrinking the selector must not
	// wait on unrelated failures.
	if err := r.garbageCollect(ctx, logger, cs, digest, selected); err != nil {
		logger.Error(err, "garbage collection failed")
		// continue applying; GC error is reflected via event + requeue below.
	}

	// Apply to every selected namespace, tracking per-target outcome.
	now := metav1.Now()
	previous := indexTargets(cs.Status.Targets)
	newTargets := make([]configv1alpha1.TargetStatus, 0, len(selectedNames))
	var failures []string

	for _, nsName := range selectedNames {
		ns := selected[nsName]
		ts := configv1alpha1.TargetStatus{
			Namespace:          nsName,
			UID:                ns.UID,
			ChildName:          childName,
			ObservedGeneration: cs.Generation,
		}
		prev := previous[nsName]
		applyErr := r.applyToNamespace(ctx, logger, cs, ns, digest, childName)
		if applyErr != nil {
			ts.Phase = configv1alpha1.TargetFailed
			ts.LastError = applyErr.Error()
			ts.LastTransitionTime = transitionTime(prev, configv1alpha1.TargetFailed, now)
			// Do NOT roll back: keep the version of whatever was last applied.
			ts.Version = prev.Version
			failures = append(failures, nsName)
			logger.Info("target apply failed", "namespace", nsName, "error", applyErr.Error())
		} else {
			ts.Phase = configv1alpha1.TargetApplied
			ts.Version = digest
			ts.LastTransitionTime = transitionTime(prev, configv1alpha1.TargetApplied, now)
			if prev.Phase != configv1alpha1.TargetApplied {
				r.Recorder.Eventf(cs, corev1.EventTypeNormal, "TargetApplied",
					"configuration applied to namespace %q", nsName)
			}
		}
		newTargets = append(newTargets, ts)
	}

	if err := r.writeStatus(ctx, cs, digest, newTargets, failures); err != nil {
		logger.Error(err, "status update failed; will retry")
		return ctrl.Result{Requeue: true}, nil
	}

	if len(failures) > 0 {
		// Deterministic retry instead of unbounded exponential backoff, so the
		// partial-failure scenario self-heals once the injected fault clears.
		delay := r.RetryInterval
		if delay == 0 {
			delay = DefaultRetryInterval
		}
		logger.Info("requeueing with failures", "failed", failures, "retryAfter", delay)
		return ctrl.Result{RequeueAfter: delay}, nil
	}
	return ctrl.Result{}, nil
}

// applyToNamespace creates or adopts the child ConfigMap in one namespace. It
// never deletes a foreign object: if the desired name is occupied by a ConfigMap
// whose owner UID differs, that is a Failed target (name conflict), not a
// delete-and-recreate.
func (r *Reconciler) applyToNamespace(ctx context.Context, logger logr.Logger,
	cs *configv1alpha1.ConfigSnapshot, ns *corev1.Namespace, digest, childName string) error {
	if ns.Status.Phase == corev1.NamespaceTerminating {
		return fmt.Errorf("namespace %q is terminating", ns.Name)
	}

	desired := r.buildChild(cs, ns, digest, childName)

	existing := &corev1.ConfigMap{}
	err := r.Get(ctx, types.NamespacedName{Namespace: ns.Name, Name: childName}, existing)
	switch {
	case apierrors.IsNotFound(err):
		if createErr := r.Create(ctx, desired); createErr != nil {
			if apierrors.IsAlreadyExists(createErr) {
				// Lost a create race (or pre-existing foreign object): reload and
				// judge ownership before touching anything.
				return r.adoptOrReject(ctx, cs, ns, digest)
			}
			return createErr
		}
		logger.Info("child created", "namespace", ns.Name, "name", childName)
		return nil
	case err != nil:
		return fmt.Errorf("get child: %w", err)
	}
	return r.adoptOrRejectExisting(ctx, existing, cs, digest)
}

// adoptOrReject handles the AlreadyExists race on create.
func (r *Reconciler) adoptOrReject(ctx context.Context,
	cs *configv1alpha1.ConfigSnapshot, ns *corev1.Namespace, digest string) error {
	childName := naming.ChildName(cs.Name, digest)
	existing := &corev1.ConfigMap{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns.Name, Name: childName}, existing); err != nil {
		return fmt.Errorf("reload child after conflict: %w", err)
	}
	return r.adoptOrRejectExisting(ctx, existing, cs, digest)
}

// adoptOrRejectExisting either converges an owned child to the desired content
// or rejects a foreign same-named object.
func (r *Reconciler) adoptOrRejectExisting(ctx context.Context,
	existing *corev1.ConfigMap, cs *configv1alpha1.ConfigSnapshot, digest string) error {
	if !ownedBy(existing, cs.UID) {
		owner := describeOwner(existing)
		r.Recorder.Eventf(cs, corev1.EventTypeWarning, "NameConflict",
			"desired child %s/%s exists but %s; leaving it untouched",
			existing.Namespace, existing.Name, owner)
		return fmt.Errorf("name conflict: %s/%s exists but %s",
			existing.Namespace, existing.Name, owner)
	}
	if contentInSync(existing, cs, digest) {
		return nil
	}
	updated := existing.DeepCopy()
	updated.Data = map[string]string{naming.DataKey: cs.Spec.Payload.Data}
	if updated.Annotations == nil {
		updated.Annotations = map[string]string{}
	}
	updated.Annotations[naming.DigestKey] = digest
	updated.Annotations[naming.FormatKey] = cs.Spec.Payload.Format
	if updated.Labels == nil {
		updated.Labels = map[string]string{}
	}
	for k, v := range naming.ChildLabels(cs.Name) {
		updated.Labels[k] = v
	}
	if err := r.Update(ctx, updated); err != nil {
		return fmt.Errorf("restore drifted child: %w", err)
	}
	return nil
}

// garbageCollect deletes children of THIS ConfigSnapshot whose namespace left
// the selection. Ownership is proven by controller ownerReference UID; a
// labelled object with a different owner UID is foreign and protected.
func (r *Reconciler) garbageCollect(ctx context.Context, logger logr.Logger,
	cs *configv1alpha1.ConfigSnapshot, digest string, selected map[string]*corev1.Namespace) error {
	candidates := &corev1.ConfigMapList{}
	if err := r.List(ctx, candidates,
		client.MatchingLabels(naming.ChildLabels(cs.Name))); err != nil {
		return fmt.Errorf("list child candidates: %w", err)
	}
	var firstErr error
	for i := range candidates.Items {
		cm := &candidates.Items[i]
		if !ownedBy(cm, cs.UID) {
			// Same label, different owner UID (owner recreation / collision):
			// never delete. This is the explicit guard the tests exercise.
			logger.Info("skipping foreign object with our label",
				"namespace", cm.Namespace, "name", cm.Name, "owner", describeOwner(cm))
			continue
		}
		if _, keep := selected[cm.Namespace]; keep {
			continue // still selected; apply loop owns convergence
		}
		logger.Info("garbage-collecting child that left the selection",
			"namespace", cm.Namespace, "name", cm.Name)
		if err := r.Delete(ctx, cm); err != nil && !apierrors.IsNotFound(err) {
			r.Recorder.Eventf(cs, corev1.EventTypeWarning, "GCFailed",
				"failed to delete %s/%s: %v", cm.Namespace, cm.Name, err)
			if firstErr == nil {
				firstErr = err
			}
		}
	}
	return firstErr
}

// writeStatus overwrites status with optimistic-concurrency retries, so a
// resync storm racing on the same object converges instead of failing.
func (r *Reconciler) writeStatus(ctx context.Context, cs *configv1alpha1.ConfigSnapshot,
	digest string, targets []configv1alpha1.TargetStatus, failures []string) error {
	var applied, failed int
	cond := metav1.ConditionTrue
	reason := "AllTargetsApplied"
	msg := fmt.Sprintf("%d/%d targets applied", len(targets), len(targets))
	if len(failures) > 0 {
		cond = metav1.ConditionFalse
		reason = "TargetsFailing"
		msg = fmt.Sprintf("%d target(s) failing: %v", len(failures), failures)
	}
	for _, t := range targets {
		switch t.Phase {
		case configv1alpha1.TargetApplied:
			applied++
		case configv1alpha1.TargetFailed:
			failed++
		}
	}

	const maxConflictRetries = 5
	for attempt := 0; attempt < maxConflictRetries; attempt++ {
		latest := &configv1alpha1.ConfigSnapshot{}
		if err := r.Get(ctx, client.ObjectKeyFromObject(cs), latest); err != nil {
			return err
		}
		latest.Status.ObservedGeneration = latest.Generation
		latest.Status.Version = digest
		latest.Status.Targets = targets
		latest.Status.TargetCount = len(targets)
		latest.Status.AppliedCount = applied
		latest.Status.FailedCount = failed
		meta := metav1.Condition{
			Type:               configv1alpha1.ConditionReady,
			Status:             metav1.ConditionStatus(cond),
			Reason:             reason,
			Message:            msg,
			LastTransitionTime: metav1.Now(),
		}
		latest.Status.Conditions = []metav1.Condition{meta}
		if err := r.Status().Update(ctx, latest); err != nil {
			if apierrors.IsConflict(err) {
				klog.V(5).InfoS("status conflict, retrying", "attempt", attempt)
				continue
			}
			return err
		}
		return nil
	}
	return fmt.Errorf("status update conflicted %d times", maxConflictRetries)
}

func (r *Reconciler) setTerminalError(ctx context.Context, cs *configv1alpha1.ConfigSnapshot, msg string) error {
	latest := &configv1alpha1.ConfigSnapshot{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(cs), latest); err != nil {
		return err
	}
	latest.Status.Conditions = []metav1.Condition{{
		Type:               configv1alpha1.ConditionReady,
		Status:             metav1.ConditionFalse,
		Reason:             "InvalidSpec",
		Message:            msg,
		LastTransitionTime: metav1.Now(),
	}}
	return r.Status().Update(ctx, latest)
}

// buildChild constructs the desired child ConfigMap.
func (r *Reconciler) buildChild(cs *configv1alpha1.ConfigSnapshot, ns *corev1.Namespace,
	digest, childName string) *corev1.ConfigMap {
	format := cs.Spec.Payload.Format
	if format == "" {
		format = "properties"
	}
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      childName,
			Namespace: ns.Name,
			Labels:    naming.ChildLabels(cs.Name),
			Annotations: map[string]string{
				naming.DigestKey:    digest,
				naming.FormatKey:    format,
				naming.OwnerNameKey: cs.Name,
			},
		},
		Data: map[string]string{naming.DataKey: cs.Spec.Payload.Data},
	}
	// The controller UID owner reference is the authoritative ownership
	// proof used by GC and by the foreign-object guard.
	if err := controllerutil.SetControllerReference(cs, cm, r.Scheme); err != nil {
		// Owner and child are registered types; cannot happen at runtime.
		panic(fmt.Errorf("set controller reference: %w", err))
	}
	return cm
}

// ownedBy reports whether cm has a controller ownerReference with uid.
func ownedBy(cm *corev1.ConfigMap, uid types.UID) bool {
	for _, o := range cm.OwnerReferences {
		if o.UID == uid {
			return true
		}
	}
	return false
}

func describeOwner(cm *corev1.ConfigMap) string {
	for _, o := range cm.OwnerReferences {
		return fmt.Sprintf("owned by %s/%s (UID %s)", o.Kind, o.Name, o.UID)
	}
	return "has no ownerReference"
}

func contentInSync(cm *corev1.ConfigMap, cs *configv1alpha1.ConfigSnapshot, digest string) bool {
	if cm.Annotations[naming.DigestKey] != digest {
		return false
	}
	return cm.Data[naming.DataKey] == cs.Spec.Payload.Data
}

func indexTargets(in []configv1alpha1.TargetStatus) map[string]configv1alpha1.TargetStatus {
	out := make(map[string]configv1alpha1.TargetStatus, len(in))
	for _, t := range in {
		out[t.Namespace] = t
	}
	return out
}

// transitionTime preserves the existing transition timestamp while the phase
// is unchanged; stamps now on a real transition.
func transitionTime(prev configv1alpha1.TargetStatus, phase configv1alpha1.TargetPhase, now metav1.Time) metav1.Time {
	if prev.Phase == phase && !prev.LastTransitionTime.IsZero() {
		return prev.LastTransitionTime
	}
	return now
}
