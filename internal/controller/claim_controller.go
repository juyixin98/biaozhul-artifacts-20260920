// Package controller contains the ResourceClaim and ReservationPool reconcilers.
package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"

	quota "resourcequota-reservation/api/v1alpha1"
	"resourcequota-reservation/internal/ledger"
)

// ClaimReconciler reconciles ResourceClaim objects against the per-namespace
// ReservationPool and real Pod objects.
type ClaimReconciler struct {
	client.Client
	Scheme *runtime.Scheme
	// MaxReservationRetries bounds how many optimistic-concurrency retries
	// one reconcile performs while debiting the pool. After this many
	// conflicts the claim is Rejected with ConflictExhausted so a wedged
	// pool cannot wedge every claim forever.
	MaxReservationRetries int
	// Now is overridable in tests.
	Now func() time.Time
	// ControllerName is used for manager registration; defaults to
	// "resourceclaim". Integration tests that start multiple managers in one
	// test process must set a unique name per manager.
	ControllerName string
}

const (
	// DefaultMaxReservationRetries bounds optimistic-concurrency retries.
	DefaultMaxReservationRetries = 25
	// conflictRetryBase is the backoff base between pool CAS retries.
	conflictRetryBase = 5 * time.Millisecond
)

// +kubebuilder:rbac:groups=quota.example.com,resources=resourceclaims,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=quota.example.com,resources=resourceclaims/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=quota.example.com,resources=resourceclaims/finalizers,verbs=update
// +kubebuilder:rbac:groups=quota.example.com,resources=reservationpools,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=quota.example.com,resources=reservationpools/status,verbs=get;update;patch
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=namespaces,verbs=get;list;watch

// Reconcile drives one ResourceClaim through its lifecycle.
//
// The ordering below is the heart of the expiry-vs-Pod race handling:
//
//  1. Find a live bound Pod FIRST. A real object always wins: if a Pod with
//     the claim label exists, the claim must be Bound and its capacity must
//     stay in the pool — even if the TTL has elapsed. This is the "late Pod"
//     rule. The Pod webhook is what normally prevents late Pods; the check
//     here closes the window between expiry decision and Pod admission.
//  2. Only when there is definitely no live Pod may Reserved -> Expired
//     proceed, crediting capacity back.
//  3. Every pool mutation is an optimistic-concurrency retry loop on the pool
//     object; the API server (not process memory) is the serialization point.
func (r *ClaimReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)
	now := r.now()

	var claim quota.ResourceClaim
	if err := r.Get(ctx, req.NamespacedName, &claim); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// Deletion path: release capacity (idempotent), remove finalizer.
	if !claim.DeletionTimestamp.IsZero() {
		if containsString(claim.Finalizers, quota.ClaimFinalizer) {
			if err := r.creditPool(ctx, claim.Namespace, claim.UID); err != nil {
				return ctrl.Result{}, err
			}
			claim.Finalizers = removeString(claim.Finalizers, quota.ClaimFinalizer)
			if err := r.Update(ctx, &claim); err != nil && !apierrors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
			logger.Info("released capacity and removed finalizer", "claim", claim.Name)
		}
		return ctrl.Result{}, nil
	}

	// Ensure finalizer first so a delete-during-reservation cannot leak.
	if !containsString(claim.Finalizers, quota.ClaimFinalizer) {
		claim.Finalizers = append(claim.Finalizers, quota.ClaimFinalizer)
		if err := r.Update(ctx, &claim); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	cpu, mem, err := ledger.ValidateClaimSpec(claim.Spec)
	if err != nil {
		return r.markRejected(ctx, &claim, "invalid spec: "+err.Error())
	}

	// Step 1 (documented above): does a real Pod bound to this claim exist?
	pod, podErr := r.findBoundPod(ctx, &claim)
	if podErr != nil {
		return ctrl.Result{}, podErr
	}

	switch claim.Status.Phase {
	case "", quota.PhasePending:
		return r.reserve(ctx, &claim, cpu, mem, now)
	case quota.PhaseReserved:
		if pod != nil {
			return r.markBound(ctx, &claim, pod.Name)
		}
		if now.After(claim.Status.ExpiresAt.Time) || now.Equal(claim.Status.ExpiresAt.Time) {
			return r.expire(ctx, &claim)
		}
		// Wake up precisely at expiry.
		return ctrl.Result{RequeueAfter: claim.Status.ExpiresAt.Sub(now)}, nil
	case quota.PhaseBound:
		if pod != nil {
			if claim.Status.BoundPod != pod.Name {
				return r.markBound(ctx, &claim, pod.Name)
			}
			// Healthy: real object exists, capacity remains reserved.
			return ctrl.Result{}, nil
		}
		// Bound Pod disappeared. Re-evaluate: if the claim TTL has not
		// expired (from reservation time), allow rebinding; otherwise expire.
		if now.After(claim.Status.ExpiresAt.Time) || now.Equal(claim.Status.ExpiresAt.Time) {
			return r.expire(ctx, &claim)
		}
		return r.toReserved(ctx, &claim, "bound pod vanished; awaiting replacement within ttl")
	case quota.PhaseExpired:
		// Late-pod recovery: a Pod admitted in the release window exists, so
		// the release is rolled forward — re-debit (may fail with capacity
		// error, in which case the webhook should have prevented it; surface
		// the condition and leave the object for an operator).
		if pod != nil {
			return r.redebitAfterLatePod(ctx, &claim, cpu, mem, pod.Name)
		}
		return ctrl.Result{}, nil
	case quota.PhaseRejected:
		return ctrl.Result{}, nil
	default:
		return ctrl.Result{}, fmt.Errorf("unknown phase %q", claim.Status.Phase)
	}
}

// reserve debits the pool with optimistic concurrency and flips Pending->Reserved.
func (r *ClaimReconciler) reserve(ctx context.Context, claim *quota.ResourceClaim, cpu, mem int64, now time.Time) (ctrl.Result, error) {
	maxRetry := r.MaxReservationRetries
	if maxRetry == 0 {
		maxRetry = DefaultMaxReservationRetries
	}

	var conflicts int32
	var lastErr error
	success := false
	for attempt := 0; attempt < maxRetry; attempt++ {
		pool, err := r.getOrInitPool(ctx, claim.Namespace)
		if err != nil {
			return ctrl.Result{}, err
		}
		changed, derr := ledger.EnsureDebited(pool, claim.UID, claim.Name, cpu, mem)
		if derr != nil {
			// Insufficient capacity is a terminal rejection, not a retry.
			if _, ok := derr.(*ledger.ErrInsufficientCapacity); ok {
				return r.markRejected(ctx, claim, derr.Error())
			}
			return ctrl.Result{}, derr
		}
		if !changed {
			success = true // already debited (retry after prior partial success)
			break
		}
		// Status update with the resourceVersion we read: the API server
		// aborts the transaction if another client won the race.
		uerr := r.Status().Update(ctx, pool)
		if uerr == nil {
			success = true
			break
		}
		if apierrors.IsConflict(uerr) {
			conflicts++
			lastErr = uerr
			time.Sleep(conflictRetryBase * time.Duration(1<<min(attempt, 4)))
			continue
		}
		return ctrl.Result{}, uerr
	}
	if !success {
		if claim.Status.ConflictCount != conflicts {
			claim.Status.ConflictCount = conflicts
			_ = r.Status().Update(ctx, claim)
		}
		return r.markRejected(ctx, claim, "reservation conflict budget exhausted: "+lastErr.Error())
	}

	// Pool debit committed durably — now transition the claim.
	reservedAt := metav1.NewTime(now)
	claim.Status.Phase = quota.PhaseReserved
	claim.Status.ReservedCPU = ledger.FormatMilliCPU(cpu)
	claim.Status.ReservedMemory = ledger.FormatBytes(mem)
	claim.Status.ReservedAt = &reservedAt
	expires := metav1.NewTime(now.Add(claim.Spec.TTL.Duration))
	claim.Status.ExpiresAt = &expires
	claim.Status.ConflictCount = conflicts
	claim.Status.Message = ""
	claim.Status.ObservedGeneration = claim.Generation
	if err := r.Status().Update(ctx, claim); err != nil {
		if apierrors.IsConflict(err) {
			// Claim object itself moved (rare); requeue and reconcile from the
			// fresh state — the debit is idempotent by UID.
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: claim.Spec.TTL.Duration}, nil
}

// expire performs Reserved/Bound-without-pod -> Expired and credits the pool.
func (r *ClaimReconciler) expire(ctx context.Context, claim *quota.ResourceClaim) (ctrl.Result, error) {
	// Defensive re-check inside this transition: no live Pod exists.
	pod, err := r.findBoundPod(ctx, claim)
	if err != nil {
		return ctrl.Result{}, err
	}
	if pod != nil {
		// Race won by the Pod: bind instead of expiring.
		return r.markBound(ctx, claim, pod.Name)
	}
	if err := r.creditPool(ctx, claim.Namespace, claim.UID); err != nil {
		return ctrl.Result{}, err
	}
	claim.Status.Phase = quota.PhaseExpired
	claim.Status.BoundPod = ""
	claim.Status.Message = "ttl expired without a bound pod; capacity released"
	claim.Status.ObservedGeneration = claim.Generation
	if err := r.Status().Update(ctx, claim); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// markBound transitions to Bound and records the pod name.
func (r *ClaimReconciler) markBound(ctx context.Context, claim *quota.ResourceClaim, podName string) (ctrl.Result, error) {
	claim.Status.Phase = quota.PhaseBound
	claim.Status.BoundPod = podName
	claim.Status.Message = ""
	claim.Status.ObservedGeneration = claim.Generation
	if err := r.Status().Update(ctx, claim); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// toReserved moves Bound (with missing pod) back to Reserved while TTL lives.
func (r *ClaimReconciler) toReserved(ctx context.Context, claim *quota.ResourceClaim, msg string) (ctrl.Result, error) {
	claim.Status.Phase = quota.PhaseReserved
	claim.Status.BoundPod = ""
	claim.Status.Message = msg
	claim.Status.ObservedGeneration = claim.Generation
	if err := r.Status().Update(ctx, claim); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// redebitAfterLatePod handles the Expired + existing Pod case: re-debit the
// pool (optimistic CAS) then go Bound.
func (r *ClaimReconciler) redebitAfterLatePod(ctx context.Context, claim *quota.ResourceClaim, cpu, mem int64, podName string) (ctrl.Result, error) {
	for attempt := 0; attempt < DefaultMaxReservationRetries; attempt++ {
		pool, err := r.getOrInitPool(ctx, claim.Namespace)
		if err != nil {
			return ctrl.Result{}, err
		}
		if _, err := ledger.EnsureDebited(pool, claim.UID, claim.Name, cpu, mem); err != nil {
			return ctrl.Result{}, fmt.Errorf("late pod cannot be covered: %w", err)
		}
		if err := r.Status().Update(ctx, pool); err != nil {
			if apierrors.IsConflict(err) {
				continue
			}
			return ctrl.Result{}, err
		}
		return r.markBound(ctx, claim, podName)
	}
	return ctrl.Result{}, fmt.Errorf("late-pool re-debit conflict budget exhausted")
}

// markRejected transitions to Rejected (terminal; finalizer stays harmless but
// is removed by the deletion path).
func (r *ClaimReconciler) markRejected(ctx context.Context, claim *quota.ResourceClaim, msg string) (ctrl.Result, error) {
	claim.Status.Phase = quota.PhaseRejected
	claim.Status.Message = msg
	claim.Status.ObservedGeneration = claim.Generation
	if err := r.Status().Update(ctx, claim); err != nil {
		if apierrors.IsConflict(err) {
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// creditPool removes one claim's contribution from its namespace pool.
func (r *ClaimReconciler) creditPool(ctx context.Context, ns string, uid types.UID) error {
	var pool quota.ReservationPool
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool); err != nil {
		if apierrors.IsNotFound(err) {
			return nil // nothing to credit
		}
		return err
	}
	if !ledger.Credit(&pool, uid) {
		return nil
	}
	for attempt := 0; attempt < DefaultMaxReservationRetries; attempt++ {
		err := r.Status().Update(ctx, &pool)
		if err == nil {
			return nil
		}
		if !apierrors.IsConflict(err) {
			return err
		}
		if gerr := r.Get(ctx, client.ObjectKeyFromObject(&pool), &pool); gerr != nil {
			return gerr
		}
		if !ledger.Credit(&pool, uid) {
			return nil
		}
	}
	return fmt.Errorf("credit: pool conflict budget exhausted")
}

// getOrInitPool fetches the namespace pool, creating it lazily with zero
// defaults if missing. Note: a namespace is expected to install a pool via the
// sample manifest; lazy create keeps standalone tests simple.
func (r *ClaimReconciler) getOrInitPool(ctx context.Context, ns string) (*quota.ReservationPool, error) {
	var pool quota.ReservationPool
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool)
	if err == nil {
		return &pool, nil
	}
	if !apierrors.IsNotFound(err) {
		return nil, err
	}
	pool = quota.ReservationPool{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: quota.PoolName},
		Spec: quota.PoolSpec{
			CPUCapacity:    "0",
			MemoryCapacity: "0",
		},
	}
	// Best-effort create; concurrent creators will conflict, then read below.
	if cerr := r.Create(ctx, &pool); cerr != nil && !apierrors.IsAlreadyExists(cerr) {
		return nil, cerr
	}
	if err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: quota.PoolName}, &pool); err != nil {
		return nil, err
	}
	return &pool, nil
}

// findBoundPod returns a non-terminal Pod carrying the claim label. Terminal
// Pods (Succeeded/Failed) are ignored so completed workloads release capacity.
func (r *ClaimReconciler) findBoundPod(ctx context.Context, claim *quota.ResourceClaim) (*corev1.Pod, error) {
	var pods corev1.PodList
	if err := r.List(ctx, &pods, client.InNamespace(claim.Namespace),
		client.MatchingLabels{quota.ClaimLabelKey: claim.Name}); err != nil {
		return nil, err
	}
	for i := range pods.Items {
		p := &pods.Items[i]
		if p.Status.Phase == corev1.PodSucceeded || p.Status.Phase == corev1.PodFailed {
			continue
		}
		return p, nil
	}
	return nil, nil
}

func (r *ClaimReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func containsString(slice []string, s string) bool {
	for _, v := range slice {
		if v == s {
			return true
		}
	}
	return false
}

func removeString(slice []string, s string) []string {
	out := slice[:0]
	for _, v := range slice {
		if v != s {
			out = append(out, v)
		}
	}
	return out
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// SetupWithManager wires watches: claims are primary; pods and the pool trigger
// requeue of related claims via the mappers.
func (r *ClaimReconciler) SetupWithManager(mgr ctrl.Manager) error {
	c := mgr.GetClient()
	name := r.ControllerName
	if name == "" {
		name = "resourceclaim"
	}
	return ctrl.NewControllerManagedBy(mgr).
		Named(name).
		For(&quota.ResourceClaim{}).
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(claimEnqueueMapper(c))).
		Watches(&quota.ReservationPool{}, handler.EnqueueRequestsFromMapFunc(poolToClaimsMapper(c)),
			builder.WithPredicates(poolStatusChangedPredicate{})).
		Complete(r)
}
