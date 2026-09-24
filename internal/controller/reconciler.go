// Package controller implements the Snapshot reconciler.
//
// Correctness properties enforced here (each is covered by a test):
//
//   - Idempotence: a worker Job name is a deterministic function of
//     (snapshot, generation), so repeated reconciles never create a second Job
//     for the same generation.
//   - Observed generation binding: status is only ever written for the Job
//     whose generation label equals snapshot.metadata.generation. A Job from
//     an old generation that completes late can never overwrite newer state.
//   - Recovery: if the process restarts (or crashes between Job completion and
//     status write), the next reconcile reads the Job + result ConfigMap and
//     repairs status purely from cluster state.
//   - Ordered deletion: a finalizer keeps the Snapshot alive until all owned
//     Jobs and result ConfigMaps have actually disappeared.
package controller

import (
	"context"
	"fmt"
	"strconv"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	"k8s.io/client-go/tools/record"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
)

// Reconciler reconciles Snapshot objects.
type Reconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
	JobCfg   JobImageConfig

	// RequeueWhileRunning bounds how quickly we re-check a running Job;
	// watch events usually trigger sooner.
	RequeueWhileRunning time.Duration
	// RequeuePending is used while a prerequisite (source ConfigMap) is absent.
	RequeuePending time.Duration
}

// +kubebuilder:rbac:groups=snapshot.example.com,resources=snapshots,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=snapshot.example.com,resources=snapshots/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=snapshot.example.com,resources=snapshots/finalizers,verbs=update
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups="",resources=serviceaccounts,verbs=get;list;watch

func (r *Reconciler) defaults() {
	if r.RequeueWhileRunning == 0 {
		r.RequeueWhileRunning = 15 * time.Second
	}
	if r.RequeuePending == 0 {
		r.RequeuePending = 10 * time.Second
	}
}

// Reconcile implements reconcile.Reconciler.
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	r.defaults()
	logger := log.FromContext(ctx).WithValues("snapshot", req.NamespacedName)

	var snap snapshotv1alpha1.Snapshot
	if err := r.Get(ctx, req.NamespacedName, &snap); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// Deletion path: drain dependents before dropping the finalizer.
	if !snap.DeletionTimestamp.IsZero() {
		return r.handleDeletion(ctx, &snap, logger)
	}

	// Ensure the finalizer is present on first reconcile.
	if err := r.ensureFinalizer(ctx, &snap); err != nil {
		return ctrl.Result{}, err
	}

	// 1. Garbage collect resources from older generations. If anything was
	//    deleted here, requeue: a new Job must not be created in the same pass
	//    while old Pods are terminating, and old state must not win a race.
	cleaned, err := r.gcStaleGenerations(ctx, &snap, logger)
	if err != nil {
		return ctrl.Result{}, err
	}
	if cleaned {
		return ctrl.Result{Requeue: true}, nil
	}

	// 2. The source ConfigMap must exist; otherwise we stay Pending.
	src := &corev1.ConfigMap{}
	srcErr := r.Get(ctx, types.NamespacedName{Namespace: snap.Namespace, Name: snap.Spec.SourceConfigMap}, src)
	if apierrors.IsNotFound(srcErr) {
		if err := r.patchPhaseIfNewer(ctx, &snap, snapshotv1alpha1.PhasePending, "SourceMissing",
			fmt.Sprintf("source ConfigMap %q not found", snap.Spec.SourceConfigMap)); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.RequeuePending}, nil
	}
	if srcErr != nil {
		return ctrl.Result{}, srcErr
	}

	// 3. Locate (or create) the deterministic Job for the current generation.
	jobName := generationName(snap.Name, snap.Generation)
	var job batchv1.Job
	err = r.Get(ctx, types.NamespacedName{Namespace: snap.Namespace, Name: jobName}, &job)
	switch {
	case apierrors.IsNotFound(err):
		// No Job for this generation yet: create it exactly once, then publish
		// Running status bound to this generation before requeuing.
		newJob := buildWorkerJob(&snap, r.JobCfg)
		if createErr := r.Create(ctx, newJob); createErr != nil {
			if apierrors.IsAlreadyExists(createErr) {
				// Lost the race with a duplicate reconcile event; re-read.
				return ctrl.Result{Requeue: true}, nil
			}
			return ctrl.Result{}, createErr
		}
		logger.Info("created worker job", "job", newJob.Name, "generation", snap.Generation)
		r.eventf(&snap, "Normal", "JobCreated", "Created worker job %q for generation %d", newJob.Name, snap.Generation)

		updated := snap.DeepCopy()
		updated.Status.JobRef = &snapshotv1alpha1.JobRef{Name: jobName, Generation: snap.Generation}
		setPhase(&updated.Status, updated, snapshotv1alpha1.PhaseRunning)
		if err := r.statusUpdate(ctx, &snap, updated); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.RequeueWhileRunning}, nil

	case err != nil:
		return ctrl.Result{}, err
	}

	// The Job we found must belong to THIS generation. The name encodes it,
	// but labels are the authoritative check (defends against hand edits).
	if genOf(&job) != snap.Generation {
		logger.Info("found job with mismatched generation; deleting",
			"job", job.Name, "jobGeneration", genOf(&job), "snapshotGeneration", snap.Generation)
		if err := r.Delete(ctx, &job, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	// 4. Reflect Running for in-flight Jobs (also recovers a missing status).
	limit := r.JobCfg.BackoffLimit
	if job.Spec.BackoffLimit != nil {
		limit = *job.Spec.BackoffLimit
	}
	if job.Status.Succeeded == 0 && job.Status.Failed <= limit && !jobConditionTrue(&job, batchv1.JobFailed) {
		if snap.Status.Phase != snapshotv1alpha1.PhaseRunning || snap.Status.ObservedGeneration != snap.Generation {
			updated := snap.DeepCopy()
			updated.Status.JobRef = &snapshotv1alpha1.JobRef{Name: job.Name, Generation: snap.Generation}
			setPhase(&updated.Status, updated, snapshotv1alpha1.PhaseRunning)
			if err := r.statusUpdate(ctx, &snap, updated); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{RequeueAfter: r.RequeueWhileRunning}, nil
	}

	// 5. Terminal Job. Success requires the result ConfigMap; failure is
	//    terminal per generation (spec change => new generation => new Job).
	if job.Status.Succeeded >= 1 {
		return r.reconcileSuccess(ctx, &snap, &job, logger)
	}

	return r.reconcileFailure(ctx, &snap, &job, logger)
}

// reconcileSuccess reads the result ConfigMap and records Ready — but only if
// the result was produced for the current generation. This is the stale-write
// guard: an old Job completing late cannot advance status for a new gen.
func (r *Reconciler) reconcileSuccess(ctx context.Context, snap *snapshotv1alpha1.Snapshot, job *batchv1.Job, logger interface {
	Info(msg string, keysAndValues ...interface{})
}) (ctrl.Result, error) {
	var result corev1.ConfigMap
	err := r.Get(ctx, types.NamespacedName{Namespace: snap.Namespace, Name: job.Name}, &result)
	switch {
	case apierrors.IsNotFound(err):
		// Job succeeded but the result object vanished (or never made it before
		// pod teardown). Mark Failed for this generation rather than spinning.
		return r.failGeneration(ctx, snap, job, "ResultMissing",
			"worker job completed but result ConfigMap was not found")
	case err != nil:
		return ctrl.Result{}, err
	}

	// Generation guard on the result object itself.
	if genOf(&result) != snap.Generation {
		logger.Info("ignoring result ConfigMap from an older generation",
			"configmap", result.Name, "resultGeneration", genOf(&result), "snapshotGeneration", snap.Generation)
		return ctrl.Result{RequeueAfter: r.RequeueWhileRunning}, nil
	}

	parsed, err := parseResultConfigMap(result.Data)
	if err != nil {
		return r.failGeneration(ctx, snap, job, "ResultInvalid", err.Error())
	}
	if parsed.generation != 0 && parsed.generation != snap.Generation {
		logger.Info("ignoring result payload from an older generation",
			"payloadGeneration", parsed.generation, "snapshotGeneration", snap.Generation)
		return ctrl.Result{RequeueAfter: r.RequeueWhileRunning}, nil
	}

	updated := snap.DeepCopy()
	setDigestStatus(&updated.Status, updated, parsed.digest, parsed.algorithm, parsed.manifest,
		parsed.fileCount, parsed.totalBytes, job.Name)
	if err := r.statusUpdate(ctx, snap, updated); err != nil {
		return ctrl.Result{}, err
	}
	logger.Info("snapshot ready", "digest", parsed.digest, "generation", snap.Generation)
	r.eventf(snap, "Normal", "Ready", "Snapshot digest recorded for generation %d", snap.Generation)
	return ctrl.Result{}, nil
}

// reconcileFailure marks a generation Failed once the Job has exhausted retries.
func (r *Reconciler) reconcileFailure(ctx context.Context, snap *snapshotv1alpha1.Snapshot, job *batchv1.Job, logger interface {
	Info(msg string, keysAndValues ...interface{})
}) (ctrl.Result, error) {
	reason, message := failureFromJob(job)
	updated := snap.DeepCopy()
	setFailure(&updated.Status, updated, reason, message, job.Name)
	if err := r.statusUpdate(ctx, snap, updated); err != nil {
		return ctrl.Result{}, err
	}
	logger.Info("snapshot failed", "reason", reason, "generation", snap.Generation)
	r.eventf(snap, "Warning", reason, message)
	return ctrl.Result{}, nil
}

func (r *Reconciler) failGeneration(ctx context.Context, snap *snapshotv1alpha1.Snapshot, job *batchv1.Job, reason, message string) (ctrl.Result, error) {
	updated := snap.DeepCopy()
	setFailure(&updated.Status, updated, reason, message, job.Name)
	if err := r.statusUpdate(ctx, snap, updated); err != nil {
		return ctrl.Result{}, err
	}
	r.eventf(snap, "Warning", reason, message)
	return ctrl.Result{}, nil
}

// failureFromJob extracts a concise, deterministic failure description.
func failureFromJob(job *batchv1.Job) (string, string) {
	if c := jobCondition(job, batchv1.JobFailed); c != nil && c.Status == corev1.ConditionTrue {
		if c.Reason != "" {
			return c.Reason, c.Message
		}
	}
	return "JobFailed", fmt.Sprintf("worker job %q exhausted its retries", job.Name)
}

func jobConditionTrue(job *batchv1.Job, t batchv1.JobConditionType) bool {
	c := jobCondition(job, t)
	return c != nil && c.Status == corev1.ConditionTrue
}

func jobCondition(job *batchv1.Job, t batchv1.JobConditionType) *batchv1.JobCondition {
	for i := range job.Status.Conditions {
		if job.Status.Conditions[i].Type == t {
			return &job.Status.Conditions[i]
		}
	}
	return nil
}

// ensureFinalizer attaches the deletion finalizer exactly once.
func (r *Reconciler) ensureFinalizer(ctx context.Context, snap *snapshotv1alpha1.Snapshot) error {
	if containsString(snap.Finalizers, Finalizer) {
		return nil
	}
	patch := client.MergeFrom(snap.DeepCopy())
	snap.Finalizers = append(snap.Finalizers, Finalizer)
	return r.Patch(ctx, snap, patch)
}

// handleDeletion waits for every owned Job and result ConfigMap (across all
// generations) to be gone, then removes the finalizer.
func (r *Reconciler) handleDeletion(ctx context.Context, snap *snapshotv1alpha1.Snapshot, logger interface {
	Info(msg string, keysAndValues ...interface{})
}) (ctrl.Result, error) {
	if !containsString(snap.Finalizers, Finalizer) {
		return ctrl.Result{}, nil
	}

	jobList := &batchv1.JobList{}
	if err := r.List(ctx, jobList, client.InNamespace(snap.Namespace),
		client.MatchingLabelsSelector{Selector: snapshotSelector(snap.Name)}); err != nil {
		return ctrl.Result{}, err
	}
	cmList := &corev1.ConfigMapList{}
	if err := r.List(ctx, cmList, client.InNamespace(snap.Namespace),
		client.MatchingLabelsSelector{Selector: snapshotSelector(snap.Name)}); err != nil {
		return ctrl.Result{}, err
	}

	remaining := 0
	// Issue deletes for anything that still exists; tolerate NotFound races.
	for i := range jobList.Items {
		job := &jobList.Items[i]
		if job.DeletionTimestamp.IsZero() {
			if err := r.Delete(ctx, job, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
		}
		remaining++
	}
	for i := range cmList.Items {
		cm := &cmList.Items[i]
		if cm.DeletionTimestamp.IsZero() {
			if err := r.Delete(ctx, cm); err != nil && !apierrors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
		}
		remaining++
	}

	if remaining > 0 {
		logger.Info("deletion blocked: waiting for dependents", "remaining", remaining)
		// Watches on jobs/configmaps requeue us promptly; this is a safety net.
		return ctrl.Result{RequeueAfter: 3 * time.Second}, nil
	}

	patch := client.MergeFrom(snap.DeepCopy())
	snap.Finalizers = removeString(snap.Finalizers, Finalizer)
	if err := r.Patch(ctx, snap, patch); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	logger.Info("finalizer removed; snapshot deleted")
	return ctrl.Result{}, nil
}

// gcStaleGenerations deletes owned Jobs/result ConfigMaps whose generation
// label differs from the snapshot's current generation. Returns true when it
// deleted at least one object, so the caller can requeue and avoid letting a
// late old-generation result overwrite new-generation state.
func (r *Reconciler) gcStaleGenerations(ctx context.Context, snap *snapshotv1alpha1.Snapshot, logger interface {
	Info(msg string, keysAndValues ...interface{})
}) (bool, error) {
	jobList := &batchv1.JobList{}
	if err := r.List(ctx, jobList, client.InNamespace(snap.Namespace),
		client.MatchingLabelsSelector{Selector: snapshotSelector(snap.Name)}); err != nil {
		return false, err
	}
	cmList := &corev1.ConfigMapList{}
	if err := r.List(ctx, cmList, client.InNamespace(snap.Namespace),
		client.MatchingLabelsSelector{Selector: snapshotSelector(snap.Name)}); err != nil {
		return false, err
	}

	deleted := false
	currentGen := strconv.FormatInt(snap.Generation, 10)
	for i := range jobList.Items {
		job := &jobList.Items[i]
		if job.Labels[labelGeneration] != currentGen {
			logger.Info("deleting stale-generation job", "job", job.Name,
				"jobGeneration", job.Labels[labelGeneration], "currentGeneration", currentGen)
			if err := r.Delete(ctx, job, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
				return false, err
			}
			deleted = true
		}
	}
	for i := range cmList.Items {
		cm := &cmList.Items[i]
		if cm.Labels[labelGeneration] != currentGen {
			logger.Info("deleting stale-generation result configmap", "configmap", cm.Name,
				"generation", cm.Labels[labelGeneration], "currentGeneration", currentGen)
			if err := r.Delete(ctx, cm); err != nil && !apierrors.IsNotFound(err) {
				return false, err
			}
			deleted = true
		}
	}
	return deleted, nil
}

// patchPhaseIfNewer moves to a non-terminal waiting phase (Pending) only when
// status lags the current generation, so older Pending writes never regress
// Running/Ready of the current generation.
func (r *Reconciler) patchPhaseIfNewer(ctx context.Context, snap *snapshotv1alpha1.Snapshot, phase snapshotv1alpha1.SnapshotPhase, reason, message string) error {
	if snap.Status.ObservedGeneration == snap.Generation && snap.Status.Phase == phase {
		return nil
	}
	updated := snap.DeepCopy()
	updated.Status.FailureReason = reason
	updated.Status.FailureMessage = message
	setPhase(&updated.Status, updated, phase)
	return r.statusUpdate(ctx, snap, updated)
}

// statusUpdate writes status with optimistic concurrency. On conflict it simply
// returns the conflict error; the triggering watch requeues and the next pass
// re-reads fresh state, which prevents stale overwrites.
func (r *Reconciler) statusUpdate(ctx context.Context, old, updated *snapshotv1alpha1.Snapshot) error {
	if !statusSemanticallyChanged(old, updated) {
		return nil
	}
	if err := r.Status().Update(ctx, updated); err != nil {
		if apierrors.IsConflict(err) {
			return err
		}
		return err
	}
	return nil
}

func statusSemanticallyChanged(old, updated *snapshotv1alpha1.Snapshot) bool {
	return old.Status.ObservedGeneration != updated.Status.ObservedGeneration ||
		old.Status.Phase != updated.Status.Phase ||
		old.Status.Digest != updated.Status.Digest ||
		old.Status.Manifest != updated.Status.Manifest ||
		old.Status.FileCount != updated.Status.FileCount ||
		old.Status.TotalBytes != updated.Status.TotalBytes ||
		old.Status.FailureReason != updated.Status.FailureReason ||
		old.Status.FailureMessage != updated.Status.FailureMessage ||
		(old.Status.JobRef == nil) != (updated.Status.JobRef == nil) ||
		(old.Status.JobRef != nil && updated.Status.JobRef != nil && *old.Status.JobRef != *updated.Status.JobRef) ||
		readyConditionStatus(old.Status.Conditions) != readyConditionStatus(updated.Status.Conditions)
}

func readyConditionStatus(conds []metav1.Condition) metav1.ConditionStatus {
	if c := meta.FindStatusCondition(conds, condReady); c != nil {
		return c.Status
	}
	return ""
}

func (r *Reconciler) eventf(obj client.Object, eventtype, reason, fmtMsg string, args ...interface{}) {
	if r.Recorder != nil {
		r.Recorder.Eventf(obj, eventtype, reason, fmtMsg, args...)
	}
}

// genOf reads the generation label off a labelled object.
type labelled interface {
	GetLabels() map[string]string
}

func genOf(obj labelled) int64 {
	v, _ := strconv.ParseInt(obj.GetLabels()[labelGeneration], 10, 64)
	return v
}

func snapshotSelector(name string) labels.Selector {
	set := labels.Set{
		labelPartOf:    partOfValue,
		labelSnapshot:  name,
		labelManagedBy: managedBy,
	}
	return labels.SelectorFromSet(set)
}

func containsString(slice []string, s string) bool {
	for _, item := range slice {
		if item == s {
			return true
		}
	}
	return false
}

func removeString(slice []string, s string) []string {
	out := slice[:0]
	for _, item := range slice {
		if item != s {
			out = append(out, item)
		}
	}
	return out
}

// SetupWithManager wires watches. Jobs and ConfigMaps owned/labelled for a
// Snapshot enqueue that Snapshot. Watching the result ConfigMap is what wakes
// the controller the instant the worker publishes a digest.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	mapToSnapshot := func(ctx context.Context, obj client.Object) []reconcile.Request {
		snapName := obj.GetLabels()[labelSnapshot]
		if snapName == "" {
			return nil
		}
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: obj.GetNamespace(), Name: snapName}}}
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&snapshotv1alpha1.Snapshot{}).
		Owns(&batchv1.Job{}).
		Watches(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(mapToSnapshot),
			builder.WithPredicates(snapshotLabelledPredicate())).
		Complete(r)
}
