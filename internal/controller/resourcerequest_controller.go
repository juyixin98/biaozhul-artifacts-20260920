// Package controller reconciles ResourceRequest objects against QuotaPool
// ledgers using optimistic concurrency.
package controller

import (
	"context"
	"errors"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	quotav1alpha1 "github.com/biaozhul/quota-reserver/api/v1alpha1"
	"github.com/biaozhul/quota-reserver/pkg/reserve"
)

// boundPodPollInterval is how often a Bound request re-checks its pod.
const boundPodPollInterval = 15 * time.Second

// ResourceRequestReconciler drives the ResourceRequest state machine.
type ResourceRequestReconciler struct {
	client.Client
	// APIReader is a direct (uncached) reader used for decisions that must
	// not observe stale cache state — e.g. checking whether a bound pod
	// already exists right after admission created it. Nil falls back to
	// the cached Client (unit tests).
	APIReader client.Reader
	Scheme    *runtime.Scheme
	Recorder  record.EventRecorder
	// ControllerName overrides the controller's registration name; useful when
	// multiple managers run in one process (integration tests). Empty = default.
	ControllerName string
	// Now returns the current time; replaceable in tests. Nil means time.Now.
	Now func() time.Time
}

// apiReader returns the uncached reader if configured, else the client.
func (r *ResourceRequestReconciler) apiReader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

func (r *ResourceRequestReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// +kubebuilder:rbac:groups=quota.biaozhu.dev,resources=resourcerequests,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=quota.biaozhu.dev,resources=resourcerequests/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=quota.biaozhu.dev,resources=resourcerequests/finalizers,verbs=update
// +kubebuilder:rbac:groups=quota.biaozhu.dev,resources=quotapools,verbs=get;list;watch
// +kubebuilder:rbac:groups=quota.biaozhu.dev,resources=quotapools/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;delete
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile drives one ResourceRequest through its state machine.
func (r *ResourceRequestReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var rr quotav1alpha1.ResourceRequest
	if err := r.Get(ctx, req.NamespacedName, &rr); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Deletion: release any held quota, then drop the finalizer.
	if !rr.DeletionTimestamp.IsZero() {
		if controllerutil.ContainsFinalizer(&rr, quotav1alpha1.Finalizer) {
			if err := r.releaseQuota(ctx, &rr); err != nil {
				return ctrl.Result{}, err
			}
			controllerutil.RemoveFinalizer(&rr, quotav1alpha1.Finalizer)
			if err := r.Update(ctx, &rr); err != nil {
				return ctrl.Result{}, err
			}
			log.Info("finalizer released quota", "request", req.NamespacedName)
		}
		return ctrl.Result{}, nil
	}

	// Ensure the finalizer before any quota can be held.
	if !controllerutil.ContainsFinalizer(&rr, quotav1alpha1.Finalizer) {
		controllerutil.AddFinalizer(&rr, quotav1alpha1.Finalizer)
		if err := r.Update(ctx, &rr); err != nil {
			return ctrl.Result{}, err
		}
	}

	switch rr.Status.Phase {
	case "", quotav1alpha1.PhasePending:
		return r.reconcilePending(ctx, &rr)
	case quotav1alpha1.PhaseReserved:
		return r.reconcileReserved(ctx, &rr)
	case quotav1alpha1.PhaseBound:
		return r.reconcileBound(ctx, &rr)
	case quotav1alpha1.PhaseExpired:
		return r.reconcileExpired(ctx, &rr)
	default:
		// Rejected and Released are terminal.
		return ctrl.Result{}, nil
	}
}

// reconcilePending performs the Pending -> Reserved|Rejected transition.
// The pool ledger update is resourceVersion-checked and retried on conflict,
// so concurrent reconciles cannot over-commit the pool.
func (r *ResourceRequestReconciler) reconcilePending(ctx context.Context, rr *quotav1alpha1.ResourceRequest) (ctrl.Result, error) {
	log := logf.FromContext(ctx)
	poolKey := types.NamespacedName{Namespace: rr.Namespace, Name: rr.Spec.Pool}

	var reserveErr error
	err := retry.RetryOnConflict(retry.DefaultBackoff, func() error {
		var pool quotav1alpha1.QuotaPool
		if err := r.Get(ctx, poolKey, &pool); err != nil {
			return err // e.g. pool not found: retry via error backoff, stay Pending
		}
		_, reserveErr = reserve.Reserve(&pool.Status, pool.Spec,
			rr.UID, rr.Name, rr.Spec.CPUMilli, rr.Spec.MemoryBytes)
		if reserveErr != nil {
			return nil // capacity failure is a decision, not a conflict
		}
		return r.Status().Update(ctx, &pool)
	})
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("reserving in pool %s: %w", poolKey, err)
	}
	if reserveErr != nil {
		if errors.Is(reserveErr, reserve.ErrExceedsCapacity) {
			log.Info("reservation rejected: insufficient capacity", "request", client.ObjectKeyFromObject(rr))
			r.Recorder.Event(rr, corev1.EventTypeWarning, "Rejected", reserveErr.Error())
			return ctrl.Result{}, r.mutateStatus(ctx, client.ObjectKeyFromObject(rr), func(st *quotav1alpha1.ResourceRequestStatus) {
				st.Phase = quotav1alpha1.PhaseRejected
				st.Reason = "InsufficientCapacity"
				st.Message = reserveErr.Error()
			})
		}
		return ctrl.Result{}, reserveErr
	}

	now := r.now()
	expiresAt := now.Add(time.Duration(rr.Spec.TTLSeconds) * time.Second)
	if err := r.mutateStatus(ctx, client.ObjectKeyFromObject(rr), func(st *quotav1alpha1.ResourceRequestStatus) {
		st.Phase = quotav1alpha1.PhaseReserved
		st.Reason = "QuotaReserved"
		st.Message = fmt.Sprintf("reserved %dm/%dB in pool %q until %s",
			rr.Spec.CPUMilli, rr.Spec.MemoryBytes, rr.Spec.Pool, expiresAt.UTC().Format(time.RFC3339))
		reservedAt := metav1.NewTime(now)
		st.ReservedAt = &reservedAt
		expires := metav1.NewTime(expiresAt)
		st.ExpiresAt = &expires
	}); err != nil {
		return ctrl.Result{}, err
	}
	r.Recorder.Eventf(rr, corev1.EventTypeNormal, "Reserved",
		"reserved %dm/%dB in pool %q for %ds", rr.Spec.CPUMilli, rr.Spec.MemoryBytes, rr.Spec.Pool, rr.Spec.TTLSeconds)
	// Wake up exactly at expiry; add a small grace so now >= ExpiresAt then.
	return ctrl.Result{RequeueAfter: time.Until(expiresAt) + time.Second}, nil
}

// reconcileReserved performs the Reserved -> Expired transition once the TTL
// has elapsed. The race with a pod arriving at the same moment is decided by
// resourceVersion: whichever side (this controller or the pod admission
// webhook) commits its status update first wins, and the loser observes the
// committed phase and acts accordingly.
func (r *ResourceRequestReconciler) reconcileReserved(ctx context.Context, rr *quotav1alpha1.ResourceRequest) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	if rr.Status.ExpiresAt == nil {
		// Inconsistent state (should not happen); requeue to retry.
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	now := r.now()
	if now.Before(rr.Status.ExpiresAt.Time) {
		return ctrl.Result{RequeueAfter: rr.Status.ExpiresAt.Time.Sub(now) + time.Second}, nil
	}

	if err := r.releaseQuota(ctx, rr); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.mutateStatus(ctx, client.ObjectKeyFromObject(rr), func(st *quotav1alpha1.ResourceRequestStatus) {
		st.Phase = quotav1alpha1.PhaseExpired
		st.Reason = "TTLExceeded"
		st.Message = fmt.Sprintf("no pod arrived within %ds; reservation released", rr.Spec.TTLSeconds)
	}); err != nil {
		return ctrl.Result{}, err
	}
	log.Info("reservation expired", "request", client.ObjectKeyFromObject(rr))
	r.Recorder.Eventf(rr, corev1.EventTypeWarning, "Expired",
		"reservation expired after %ds without a pod", rr.Spec.TTLSeconds)
	return ctrl.Result{}, nil
}

// reconcileBound watches the bound pod; when it terminates or disappears the
// quota is released and the request becomes Released (terminal).
func (r *ResourceRequestReconciler) reconcileBound(ctx context.Context, rr *quotav1alpha1.ResourceRequest) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	if rr.Status.BoundPod == "" {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	podKey := types.NamespacedName{Namespace: rr.Namespace, Name: rr.Status.BoundPod}
	var pod corev1.Pod
	// Uncached read: the pod was created moments ago at admission time; a
	// lagging cache must never trick us into releasing live quota.
	err := r.apiReader().Get(ctx, podKey, &pod)
	switch {
	case apierrors.IsNotFound(err):
		// Pod deleted: release.
	case err != nil:
		return ctrl.Result{}, err
	case pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed:
		// Pod terminated: release.
	default:
		return ctrl.Result{RequeueAfter: boundPodPollInterval}, nil
	}

	if err := r.releaseQuota(ctx, rr); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.mutateStatus(ctx, client.ObjectKeyFromObject(rr), func(st *quotav1alpha1.ResourceRequestStatus) {
		st.Phase = quotav1alpha1.PhaseReleased
		st.Reason = "PodFinished"
		st.Message = fmt.Sprintf("bound pod %q finished or was deleted; reservation released", rr.Status.BoundPod)
	}); err != nil {
		return ctrl.Result{}, err
	}
	log.Info("reservation released after pod completion", "request", client.ObjectKeyFromObject(rr))
	r.Recorder.Eventf(rr, corev1.EventTypeNormal, "Released",
		"bound pod %q finished; reservation released", rr.Status.BoundPod)
	return ctrl.Result{}, nil
}

// reconcileExpired sweeps "late pods": pods that reference an already-expired
// request. The admission webhook rejects such pods, so this is a defense-in-
// depth sweep for races (e.g. webhook temporarily bypassed) — an explicit
// terminal-state cleanup, not the primary mechanism.
func (r *ResourceRequestReconciler) reconcileExpired(ctx context.Context, rr *quotav1alpha1.ResourceRequest) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var pods corev1.PodList
	if err := r.apiReader().List(ctx, &pods,
		client.InNamespace(rr.Namespace),
		client.MatchingLabels{quotav1alpha1.LabelRequest: rr.Name}); err != nil {
		return ctrl.Result{}, err
	}
	for i := range pods.Items {
		pod := &pods.Items[i]
		if !pod.DeletionTimestamp.IsZero() {
			continue
		}
		if err := r.Delete(ctx, pod); client.IgnoreNotFound(err) != nil {
			return ctrl.Result{}, err
		}
		log.Info("deleted late pod of expired request", "request", client.ObjectKeyFromObject(rr), "pod", pod.Name)
		r.Recorder.Eventf(rr, corev1.EventTypeWarning, "LatePodDeleted",
			"deleted pod %q that arrived after the reservation expired", pod.Name)
	}
	return ctrl.Result{}, nil
}

// releaseQuota removes this request's allocation from its pool ledger.
// Idempotent: a missing pool or missing ledger entry is a no-op.
func (r *ResourceRequestReconciler) releaseQuota(ctx context.Context, rr *quotav1alpha1.ResourceRequest) error {
	poolKey := types.NamespacedName{Namespace: rr.Namespace, Name: rr.Spec.Pool}
	return retry.RetryOnConflict(retry.DefaultBackoff, func() error {
		var pool quotav1alpha1.QuotaPool
		if err := r.Get(ctx, poolKey, &pool); err != nil {
			return client.IgnoreNotFound(err)
		}
		if !reserve.Release(&pool.Status, rr.UID) {
			return nil // already released
		}
		return r.Status().Update(ctx, &pool)
	})
}

// mutateStatus applies fn to the request's status with conflict retry, always
// operating on the freshest resourceVersion.
func (r *ResourceRequestReconciler) mutateStatus(ctx context.Context, key client.ObjectKey,
	fn func(*quotav1alpha1.ResourceRequestStatus)) error {
	return retry.RetryOnConflict(retry.DefaultBackoff, func() error {
		var latest quotav1alpha1.ResourceRequest
		if err := r.Get(ctx, key, &latest); err != nil {
			return err
		}
		fn(&latest.Status)
		latest.Status.ObservedGeneration = latest.Generation
		return r.Status().Update(ctx, &latest)
	})
}

// podToRequest maps a pod event to the ResourceRequest named by its label.
func (r *ResourceRequestReconciler) podToRequest(ctx context.Context, obj client.Object) []reconcile.Request {
	name := obj.GetLabels()[quotav1alpha1.LabelRequest]
	if name == "" {
		return nil
	}
	return []reconcile.Request{{NamespacedName: types.NamespacedName{
		Namespace: obj.GetNamespace(), Name: name,
	}}}
}

// SetupWithManager registers the reconciler.
func (r *ResourceRequestReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Recorder == nil {
		r.Recorder = mgr.GetEventRecorderFor("quota-reserver")
	}
	if r.APIReader == nil {
		r.APIReader = mgr.GetAPIReader()
	}
	name := r.ControllerName
	if name == "" {
		name = "resourcerequest"
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&quotav1alpha1.ResourceRequest{}).
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(r.podToRequest)).
		Named(name).
		WithOptions(controller.Options{MaxConcurrentReconciles: 4}).
		Complete(r)
}
