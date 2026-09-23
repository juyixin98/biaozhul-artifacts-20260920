// Package controller implements the Snapshot reconciler.
//
// State machine (all writes bind status.observedGeneration to the generation
// they describe):
//
//	(new gen) Pending -> Job created -> Running -> result verified -> Ready
//	                                               \-> Job exhausted/backoff -> Failed
//	                                               \-> invalid result -> Failed
//	                                               \-> completed without result
//	                                                   within grace -> Failed
//
// Idempotency: the Job for a generation has a deterministic name and is only
// created when none exists; repeated Reconciles never create duplicate Jobs.
//
// Stale-generation guard: owned Jobs/ConfigMaps carry the generation label;
// a late result from an old generation is garbage collected and never writes
// status for the newer generation.
package controller

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/clock"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
)

// workerImage is the image that actually packs and hashes the payload.
const workerImage = "snapshot-worker:dev"

// Config holds tunable reconciler timings.
type Config struct {
	// ResultGrace is how long a completed Job may stay without a verifiable
	// result ConfigMap before the Snapshot is marked Failed. It exists so a
	// genuinely missing artifact is reported instead of hanging forever.
	ResultGrace time.Duration
	// RequeueWhileRunning is the poll interval while a Job is active.
	RequeueWhileRunning time.Duration
}

// Reconciler reconciles Snapshot objects.
type Reconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Recorder record.EventRecorder
	Clock    clock.Clock
	Config   Config
}

// NewReconciler wires a reconciler with production defaults.
func NewReconciler(mgr manager.Manager, cfg Config) *Reconciler {
	if cfg.ResultGrace == 0 {
		cfg.ResultGrace = 2 * time.Minute
	}
	if cfg.RequeueWhileRunning == 0 {
		cfg.RequeueWhileRunning = 30 * time.Second
	}
	return &Reconciler{
		Client:   mgr.GetClient(),
		Scheme:   mgr.GetScheme(),
		Recorder: mgr.GetEventRecorderFor("snapshot-controller"),
		Clock:    clock.RealClock{},
		Config:   cfg,
	}
}

// +kubebuilder:rbac:groups=snapshot.example.com,resources=snapshots,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=snapshot.example.com,resources=snapshots/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=snapshot.example.com,resources=snapshots/finalizers,verbs=update
// +kubebuilder:rbac:groups=batch,resources=jobs,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile implements the core loop.
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var snap snapshotv1alpha1.Snapshot
	if err := r.Get(ctx, req.NamespacedName, &snap); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Deletion path: wait until all dependents are gone before removing the
	// finalizer. Dependents are listed (not assumed) so the wait is real.
	if !snap.DeletionTimestamp.IsZero() {
		return r.finalize(ctx, &snap)
	}

	// Ensure the finalizer first; requeue once to avoid acting while the
	// object mutates.
	if !controllerutil.ContainsFinalizer(&snap, FinalizerName) {
		patch := client.MergeFrom(snap.DeepCopy())
		controllerutil.AddFinalizer(&snap, FinalizerName)
		if err := r.Patch(ctx, &snap, patch); err != nil {
			return ctrl.Result{}, err
		}
		logger.V(1).Info("added finalizer")
		return ctrl.Result{Requeue: true}, nil
	}

	// A Snapshot is terminal for its current generation only when the status
	// already reflects that exact generation. A spec change bumps generation
	// and therefore reconciles again from Pending.
	gen := snap.Generation
	terminal := snap.Status.ObservedGeneration == gen &&
		(snap.Status.Phase == snapshotv1alpha1.PhaseReady ||
			snap.Status.Phase == snapshotv1alpha1.PhaseFailed)

	// Always garbage-collect dependents belonging to older generations.
	// Their late results must never touch the current generation's status.
	if err := r.gcStaleDependents(ctx, &snap, gen); err != nil {
		return ctrl.Result{}, err
	}

	if terminal {
		// Still watch the current Job (e.g. external delete).
		return r.observeCurrentJob(ctx, &snap, gen)
	}

	return r.reconcileCurrent(ctx, &snap, gen)
}

// reconcileCurrent drives the non-terminal state machine.
func (r *Reconciler) reconcileCurrent(ctx context.Context, snap *snapshotv1alpha1.Snapshot, gen int64) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var job batchv1.Job
	jobName := JobName(snap.Name, gen)
	err := r.Get(ctx, types.NamespacedName{Name: jobName, Namespace: snap.Namespace}, &job)

	switch {
	case apierrors.IsNotFound(err):
		// No Job yet: enter Pending (if not already) and create it.
		if snap.Status.Phase != snapshotv1alpha1.PhasePending || snap.Status.ObservedGeneration != gen {
			r.resetForGeneration(snap, gen)
			if err := r.commitStatus(ctx, snap); err != nil {
				return ctrl.Result{}, err
			}
		}
		newJob := r.buildJob(snap, gen)
		if err := ctrl.SetControllerReference(snap, newJob, r.Scheme); err != nil {
			return ctrl.Result{}, err
		}
		if err := r.Create(ctx, newJob); err != nil && !apierrors.IsAlreadyExists(err) {
			return ctrl.Result{}, err
		}
		r.Recorder.Eventf(snap, corev1.EventTypeNormal, "JobCreated",
			"created snapshot Job %q for generation %d", newJob.Name, gen)
		logger.Info("created snapshot Job", "job", newJob.Name, "generation", gen)
		// Requeue to observe it; informer watches will also wake us.
		return ctrl.Result{RequeueAfter: r.Config.RequeueWhileRunning}, nil

	case err != nil:
		return ctrl.Result{}, err
	}

	// Job exists. Guard: never trust a Job whose generation label is stale.
	if lg, ok := generationLabel(&job); !ok || lg != gen {
		// Should normally have been GC'd; delete defensively and retry.
		if err := r.Delete(ctx, &job, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	return r.observeCurrentJob(ctx, snap, gen)
}

// observeCurrentJob maps the existing Job state to Snapshot status.
func (r *Reconciler) observeCurrentJob(ctx context.Context, snap *snapshotv1alpha1.Snapshot, gen int64) (ctrl.Result, error) {
	var job batchv1.Job
	if err := r.Get(ctx, types.NamespacedName{Name: JobName(snap.Name, gen), Namespace: snap.Namespace}, &job); err != nil {
		if apierrors.IsNotFound(err) {
			// External deletion of a non-terminal Job: restart the generation.
			if snap.Status.ObservedGeneration == gen && snap.Status.Phase == snapshotv1alpha1.PhaseRunning {
				snap.Status.Phase = snapshotv1alpha1.PhasePending
				snap.Status.StartedAt = nil
				if err := r.commitStatus(ctx, snap); err != nil {
					return ctrl.Result{}, err
				}
			}
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, err
	}

	now := r.Clock.Now()

	// Failure path: Job exhausted retries.
	if cond := jobCondition(&job, batchv1.JobFailed); cond != nil {
		snap.Status.Phase = snapshotv1alpha1.PhaseFailed
		snap.Status.FailureReason = ReasonJobFailed
		snap.Status.FailureMessage = cond.Message
		snap.Status.CompletedAt = finishTime(&job, cond, now)
		if err := r.commitStatus(ctx, snap); err != nil {
			return ctrl.Result{}, err
		}
		r.Recorder.Eventf(snap, corev1.EventTypeWarning, "SnapshotFailed",
			"Job %q failed for generation %d", job.Name, gen)
		return ctrl.Result{}, nil
	}

	if jobCondition(&job, batchv1.JobComplete) == nil {
		// Still running (or pending). Publish Running status, then requeue.
		if snap.Status.Phase != snapshotv1alpha1.PhaseRunning || snap.Status.ObservedGeneration != gen {
			snap.Status.Phase = snapshotv1alpha1.PhaseRunning
			snap.Status.JobRef = job.Name
			snap.Status.StartedAt = startTime(&job, now)
			snap.Status.CompletedAt = nil
			snap.Status.FailureReason = ""
			snap.Status.FailureMessage = ""
			if err := r.commitStatus(ctx, snap); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{RequeueAfter: r.Config.RequeueWhileRunning}, nil
	}

	// Job complete. The digest must come from the real artifact, never from
	// the Job alone: fetch the result ConfigMap and verify it.
	var resultCM corev1.ConfigMap
	resErr := r.Get(ctx, types.NamespacedName{Name: ResultName(snap.Name, gen), Namespace: snap.Namespace}, &resultCM)
	if apierrors.IsNotFound(resErr) {
		// Give a grace window: informer lag or slow worker publish. Beyond it,
		// a successful Job with no artifact is a real failure to report.
		finishedAt := jobFinishTime(&job, now)
		deadline := finishedAt.Add(r.Config.ResultGrace)
		if now.Before(deadline) {
			if err := r.markWaitingForResult(ctx, snap, &job, now); err != nil {
				return ctrl.Result{}, err
			}
			return ctrl.Result{RequeueAfter: time.Until(deadline)}, nil
		}
		snap.Status.Phase = snapshotv1alpha1.PhaseFailed
		snap.Status.FailureReason = ReasonResultMissing
		snap.Status.FailureMessage = fmt.Sprintf("Job %q completed but published no result ConfigMap within %s", job.Name, r.Config.ResultGrace)
		snap.Status.CompletedAt = &metav1.Time{Time: finishedAt}
		if err := r.commitStatus(ctx, snap); err != nil {
			return ctrl.Result{}, err
		}
		r.Recorder.Eventf(snap, corev1.EventTypeWarning, "SnapshotFailed", "missing result artifact for generation %d", gen)
		return ctrl.Result{}, nil
	}
	if resErr != nil {
		return ctrl.Result{}, resErr
	}

	// Stale guard on the artifact itself.
	if lg, ok := generationLabel(&resultCM); !ok || lg != gen {
		return ctrl.Result{}, fmt.Errorf("result ConfigMap %q has stale generation label", resultCM.Name)
	}

	result, verr := parseAndVerify(&resultCM, gen, snap)
	if verr != nil {
		snap.Status.Phase = snapshotv1alpha1.PhaseFailed
		snap.Status.FailureReason = ReasonResultInvalid
		snap.Status.FailureMessage = verr.Error()
		snap.Status.CompletedAt = &metav1.Time{Time: now}
		if err := r.commitStatus(ctx, snap); err != nil {
			return ctrl.Result{}, err
		}
		r.Recorder.Eventf(snap, corev1.EventTypeWarning, "SnapshotFailed",
			"invalid result artifact for generation %d: %v", gen, verr)
		return ctrl.Result{}, nil
	}

	// Success with real digest.
	snap.Status.Phase = snapshotv1alpha1.PhaseReady
	snap.Status.JobRef = job.Name
	snap.Status.ResultConfigMap = resultCM.Name
	snap.Status.SHA256 = result.SHA256
	snap.Status.SizeBytes = result.SizeBytes
	files := make([]snapshotv1alpha1.ResultFile, 0, len(result.Files))
	for _, f := range result.Files {
		files = append(files, snapshotv1alpha1.ResultFile{Path: f.Path, Size: f.Size, SHA256: f.SHA256})
	}
	snap.Status.Files = files
	if snap.Status.StartedAt == nil {
		snap.Status.StartedAt = startTime(&job, now)
	}
	snap.Status.CompletedAt = jobFinishTime2(&job, now)
	snap.Status.FailureReason = ""
	snap.Status.FailureMessage = ""
	if err := r.commitStatus(ctx, snap); err != nil {
		return ctrl.Result{}, err
	}
	r.Recorder.Eventf(snap, corev1.EventTypeNormal, "SnapshotReady",
		"snapshot for generation %d ready: %s (%d bytes)", gen, result.SHA256, result.SizeBytes)
	return ctrl.Result{}, nil
}

// markWaitingForResult keeps the Snapshot in Running while a complete Job's
// artifact is not yet observed, without flipping observedGeneration to Ready.
func (r *Reconciler) markWaitingForResult(ctx context.Context, snap *snapshotv1alpha1.Snapshot, job *batchv1.Job, now time.Time) error {
	if snap.Status.Phase != snapshotv1alpha1.PhaseRunning || snap.Status.ObservedGeneration != snap.Generation {
		snap.Status.Phase = snapshotv1alpha1.PhaseRunning
		snap.Status.JobRef = job.Name
		snap.Status.StartedAt = startTime(job, now)
	}
	return r.commitStatus(ctx, snap)
}

// resetForGeneration clears generation-scoped state when a new generation
// starts processing.
func (r *Reconciler) resetForGeneration(snap *snapshotv1alpha1.Snapshot, gen int64) {
	snap.Status.Phase = snapshotv1alpha1.PhasePending
	snap.Status.JobRef = ""
	snap.Status.ResultConfigMap = ""
	snap.Status.SHA256 = ""
	snap.Status.SizeBytes = 0
	snap.Status.Files = nil
	snap.Status.StartedAt = nil
	snap.Status.CompletedAt = nil
	snap.Status.FailureReason = ""
	snap.Status.FailureMessage = ""
}

// finalize waits for owned dependents to be gone, then drops the finalizer.
func (r *Reconciler) finalize(ctx context.Context, snap *snapshotv1alpha1.Snapshot) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	if !controllerutil.ContainsFinalizer(snap, FinalizerName) {
		return ctrl.Result{}, nil
	}

	remaining, err := r.deleteDependents(ctx, snap)
	if err != nil {
		return ctrl.Result{}, err
	}
	if remaining > 0 {
		logger.Info("waiting for dependents to be deleted", "remaining", remaining)
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}

	patch := client.MergeFrom(snap.DeepCopy())
	controllerutil.RemoveFinalizer(snap, FinalizerName)
	if err := r.Patch(ctx, snap, patch); err != nil {
		return ctrl.Result{}, err
	}
	logger.Info("cleanup complete; removed finalizer")
	return ctrl.Result{}, nil
}

// deleteDependents issues deletions for owned Jobs and result ConfigMaps and
// returns how many still exist.
func (r *Reconciler) deleteDependents(ctx context.Context, snap *snapshotv1alpha1.Snapshot) (int, error) {
	var jobs batchv1.JobList
	if err := r.List(ctx, &jobs, client.InNamespace(snap.Namespace), client.MatchingLabels{LabelComponent: ComponentJob}); err != nil {
		return 0, err
	}
	var cms corev1.ConfigMapList
	if err := r.List(ctx, &cms, client.InNamespace(snap.Namespace), client.MatchingLabels{LabelComponent: ComponentResult}); err != nil {
		return 0, err
	}

	remaining := 0
	for i := range jobs.Items {
		j := &jobs.Items[i]
		if !metav1.IsControlledBy(j, snap) {
			continue
		}
		if j.DeletionTimestamp == nil {
			if err := r.Delete(ctx, j, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
				return 0, err
			}
		}
		remaining++
	}
	for i := range cms.Items {
		c := &cms.Items[i]
		if !metav1.IsControlledBy(c, snap) {
			continue
		}
		if c.DeletionTimestamp == nil {
			if err := r.Delete(ctx, c); err != nil && !apierrors.IsNotFound(err) {
				return 0, err
			}
		}
		remaining++
	}
	return remaining, nil
}

// gcStaleDependents removes Jobs/ConfigMaps owned by this Snapshot whose
// generation label differs from the current generation.
func (r *Reconciler) gcStaleDependents(ctx context.Context, snap *snapshotv1alpha1.Snapshot, gen int64) error {
	var jobs batchv1.JobList
	if err := r.List(ctx, &jobs, client.InNamespace(snap.Namespace), client.MatchingLabels{LabelComponent: ComponentJob}); err != nil {
		return err
	}
	for i := range jobs.Items {
		j := &jobs.Items[i]
		if !metav1.IsControlledBy(j, snap) {
			continue
		}
		if lg, ok := generationLabel(j); ok && lg == gen {
			continue
		}
		if j.DeletionTimestamp == nil {
			if err := r.Delete(ctx, j, client.PropagationPolicy(metav1.DeletePropagationBackground)); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		}
	}

	var cms corev1.ConfigMapList
	if err := r.List(ctx, &cms, client.InNamespace(snap.Namespace), client.MatchingLabels{LabelComponent: ComponentResult}); err != nil {
		return err
	}
	currentResult := ResultName(snap.Name, gen)
	for i := range cms.Items {
		c := &cms.Items[i]
		if !metav1.IsControlledBy(c, snap) {
			continue
		}
		// Keep only the current generation's deterministically named artifact.
		if lg, ok := generationLabel(c); ok && lg == gen && c.Name == currentResult {
			continue
		}
		if c.DeletionTimestamp == nil {
			if err := r.Delete(ctx, c); err != nil && !apierrors.IsNotFound(err) {
				return err
			}
		}
	}
	return nil
}

// buildJob constructs the deterministic snapshot Job for a generation.
func (r *Reconciler) buildJob(snap *snapshotv1alpha1.Snapshot, gen int64) *batchv1.Job {
	backoff := int32(2)
	if snap.Spec.BackoffLimit != nil {
		backoff = *snap.Spec.BackoffLimit
	}
	ttl := int32(3600)
	if snap.Spec.TTLSecondsAfterFinished != nil {
		ttl = *snap.Spec.TTLSecondsAfterFinished
	}
	outputName := snap.Spec.OutputName
	if strings.TrimSpace(outputName) == "" {
		outputName = "snapshot.tar.gz"
	}

	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      JobName(snap.Name, gen),
			Namespace: snap.Namespace,
			Labels:    componentLabels(ComponentJob, gen),
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            &backoff,
			TTLSecondsAfterFinished: &ttl,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: componentLabels(ComponentJob, gen),
				},
				Spec: corev1.PodSpec{
					RestartPolicy:      corev1.RestartPolicyNever,
					ServiceAccountName: "snapshot-worker",
					Volumes: []corev1.Volume{{
						Name: "work",
						VolumeSource: corev1.VolumeSource{
							EmptyDir: &corev1.EmptyDirVolumeSource{},
						},
					}},
					Containers: []corev1.Container{{
						Name:            "snapshot-worker",
						Image:           workerImage,
						ImagePullPolicy: corev1.PullIfNotPresent,
						Env: []corev1.EnvVar{
							{Name: "SNAPSHOT_NAME", Value: snap.Name},
							{Name: "SNAPSHOT_NAMESPACE", ValueFrom: &corev1.EnvVarSource{
								FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"},
							}},
							{Name: "SNAPSHOT_GENERATION", Value: fmt.Sprintf("%d", gen)},
							{Name: "SOURCE_KIND", Value: snap.Spec.Source.Kind},
							{Name: "SOURCE_NAME", Value: snap.Spec.Source.Name},
							{Name: "OUTPUT_NAME", Value: outputName},
							{Name: "RESULT_NAME", Value: ResultName(snap.Name, gen)},
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("10m"),
								corev1.ResourceMemory: resource.MustParse("32Mi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceMemory: resource.MustParse("128Mi"),
							},
						},
						VolumeMounts: []corev1.VolumeMount{{Name: "work", MountPath: "/work"}},
					}},
				},
			},
		},
	}
}

// SetupWithManager registers watches: Snapshot primary; owned Job and result
// ConfigMap secondaries (both map back to the owning Snapshot).
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return r.SetupWithName(mgr, "snapshot")
}

// SetupWithName is SetupWithManager with an explicit controller name (useful
// in tests that start several managers in one process).
func (r *Reconciler) SetupWithName(mgr ctrl.Manager, name string) error {
	return ctrl.NewControllerManagedBy(mgr).
		Named(name).
		For(&snapshotv1alpha1.Snapshot{}).
		Owns(&batchv1.Job{}).
		Watches(
			&corev1.ConfigMap{},
			resultCMMapper,
			builder.WithPredicates(predicate.NewPredicateFuncs(isResultCM)),
		).
		Complete(r)
}

// --- helpers ---------------------------------------------------------------

// parseAndVerify validates the worker's result artifact and confirms every
// claimed digest is a 32-byte hex SHA-256 and required fields are present.
// The digest values originate solely from the worker's real computation.
func parseAndVerify(cm *corev1.ConfigMap, gen int64, snap *snapshotv1alpha1.Snapshot) (*WorkerResult, error) {
	raw, ok := cm.Data[ResultKey]
	if !ok {
		return nil, fmt.Errorf("result ConfigMap %q missing key %q", cm.Name, ResultKey)
	}
	var res WorkerResult
	if err := json.Unmarshal([]byte(raw), &res); err != nil {
		return nil, fmt.Errorf("result.json is not valid JSON: %w", err)
	}
	if res.APIVersion != "snapshot.example.com/v1alpha1" || res.Kind != "SnapshotResult" {
		return nil, fmt.Errorf("unexpected result apiVersion/kind: %s/%s", res.APIVersion, res.Kind)
	}
	if res.Generation != gen {
		return nil, fmt.Errorf("result generation %d does not match expected %d (stale Job result rejected)", res.Generation, gen)
	}
	if res.SHA256 == "" || len(res.SHA256) != 64 {
		return nil, fmt.Errorf("missing or malformed archive sha256")
	}
	d, err := hex.DecodeString(res.SHA256)
	if err != nil || len(d) != 32 {
		return nil, fmt.Errorf("archive sha256 is not 32-byte hex")
	}
	if res.SizeBytes <= 0 {
		return nil, fmt.Errorf("archive size must be positive, got %d", res.SizeBytes)
	}
	if res.OutputFile == "" {
		return nil, fmt.Errorf("outputFile is empty")
	}
	if len(res.Files) == 0 {
		return nil, fmt.Errorf("result lists no files")
	}
	for _, f := range res.Files {
		if f.Path == "" {
			return nil, fmt.Errorf("result contains file with empty path")
		}
		if len(f.SHA256) != 64 {
			return nil, fmt.Errorf("file %q has malformed sha256", f.Path)
		}
		if _, err := hex.DecodeString(f.SHA256); err != nil {
			return nil, fmt.Errorf("file %q sha256 is not hex: %w", f.Path, err)
		}
		if f.Size < 0 {
			return nil, fmt.Errorf("file %q has negative size", f.Path)
		}
	}
	return &res, nil
}

// commitStatus status-updates with conflict retry. It binds
// observedGeneration to the current .metadata.generation on every non-finalize
// status write, then refreshes in-memory state.
func (r *Reconciler) commitStatus(ctx context.Context, snap *snapshotv1alpha1.Snapshot) error {
	snap.Status.ObservedGeneration = snap.Generation
	setReadyCondition(snap, r.Clock.Now())
	for attempt := 0; attempt < 3; attempt++ {
		err := r.Status().Update(ctx, snap)
		if err == nil {
			return nil
		}
		if !apierrors.IsConflict(err) {
			return err
		}
		var fresh snapshotv1alpha1.Snapshot
		if gErr := r.Get(ctx, types.NamespacedName{Name: snap.Name, Namespace: snap.Namespace}, &fresh); gErr != nil {
			return gErr
		}
		fresh.Status = snap.Status
		*snap = fresh
	}
	return fmt.Errorf("could not update status after retries")
}

// setReadyCondition maintains a standard Ready condition derived from phase.
func setReadyCondition(snap *snapshotv1alpha1.Snapshot, now time.Time) {
	var cond metav1.Condition
	switch snap.Status.Phase {
	case snapshotv1alpha1.PhaseReady:
		cond = metav1.Condition{Type: "Ready", Status: metav1.ConditionTrue, Reason: "SnapshotReady", Message: "snapshot archive produced and digest verified"}
	case snapshotv1alpha1.PhaseFailed:
		cond = metav1.Condition{Type: "Ready", Status: metav1.ConditionFalse, Reason: snap.Status.FailureReason, Message: snap.Status.FailureMessage}
	case snapshotv1alpha1.PhaseRunning:
		cond = metav1.Condition{Type: "Ready", Status: metav1.ConditionFalse, Reason: "JobRunning", Message: "snapshot Job is running"}
	default:
		cond = metav1.Condition{Type: "Ready", Status: metav1.ConditionFalse, Reason: "Pending", Message: "waiting for snapshot Job"}
	}
	cond.LastTransitionTime = metav1.NewTime(now)
	metaCond := findCondition(snap.Status.Conditions, "Ready")
	if metaCond != nil && metaCond.Status == cond.Status && metaCond.Reason == cond.Reason {
		cond.LastTransitionTime = metaCond.LastTransitionTime
	}
	snap.Status.Conditions = setCondition(snap.Status.Conditions, cond)
}

func findCondition(cs []metav1.Condition, t string) *metav1.Condition {
	for i := range cs {
		if cs[i].Type == t {
			return &cs[i]
		}
	}
	return nil
}

func setCondition(cs []metav1.Condition, c metav1.Condition) []metav1.Condition {
	for i := range cs {
		if cs[i].Type == c.Type {
			cs[i] = c
			return cs
		}
	}
	return append(cs, c)
}

func jobCondition(job *batchv1.Job, t batchv1.JobConditionType) *batchv1.JobCondition {
	for i := range job.Status.Conditions {
		if job.Status.Conditions[i].Type == t && job.Status.Conditions[i].Status == corev1.ConditionTrue {
			return &job.Status.Conditions[i]
		}
	}
	return nil
}

func startTime(job *batchv1.Job, fallback time.Time) *metav1.Time {
	if !job.Status.StartTime.IsZero() {
		return job.Status.StartTime.DeepCopy()
	}
	return &metav1.Time{Time: fallback}
}

func jobFinishTime(job *batchv1.Job, fallback time.Time) time.Time {
	if c := jobCondition(job, batchv1.JobComplete); c != nil && !c.LastTransitionTime.IsZero() {
		return c.LastTransitionTime.Time
	}
	if c := jobCondition(job, batchv1.JobFailed); c != nil && !c.LastTransitionTime.IsZero() {
		return c.LastTransitionTime.Time
	}
	return fallback
}

// jobFinishTime2 is the metav1 variant for status.
func jobFinishTime2(job *batchv1.Job, fallback time.Time) *metav1.Time {
	return &metav1.Time{Time: jobFinishTime(job, fallback)}
}

func finishTime(job *batchv1.Job, cond *batchv1.JobCondition, fallback time.Time) *metav1.Time {
	if cond != nil && !cond.LastTransitionTime.IsZero() {
		return cond.LastTransitionTime.DeepCopy()
	}
	return &metav1.Time{Time: fallback}
}
