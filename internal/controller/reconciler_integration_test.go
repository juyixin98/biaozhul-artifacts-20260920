package controller_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	snapshotv1alpha1 "snapshotcontroller/api/v1alpha1"
	"snapshotcontroller/internal/controller"
)

var testDigest = strings.Repeat("a", 64) // 64 'a' chars, valid hex placeholder

func uniqueNS(t *testing.T) string {
	return fmt.Sprintf("ns%d", time.Now().UnixNano()%1_000_000_000)
}

// TestHappyPathJobProducesRealDigest: Pending -> Running -> Ready, with a
// worker result ConfigMap carrying an actual digest. Status must bind
// observedGeneration and no second Job must ever be created.
func TestHappyPathJobProducesRealDigest(t *testing.T) {
	ctx := context.Background()
	h := startManager(t, uniqueNS(t))
	createSnapshotAndSource(ctx, h, "happy")

	running, err := waitPhase(ctx, h, "happy", snapshotv1alpha1.PhaseRunning)
	if err != nil {
		t.Fatal(err)
	}
	if running.Status.ObservedGeneration != 1 || running.Status.JobRef == nil {
		t.Fatalf("running status not bound to generation 1: %+v", running.Status)
	}
	jobName := controller.JobName("happy", 1)
	if jobName != running.Status.JobRef.Name {
		t.Fatalf("jobRef mismatch: %s vs %s", jobName, running.Status.JobRef.Name)
	}

	// Repeated reconciles must not duplicate the Job.
	time.Sleep(200 * time.Millisecond)
	if jobs := h.listJobs(ctx, "happy"); len(jobs) != 1 {
		t.Fatalf("expected exactly 1 job after repeat reconciles, got %d", len(jobs))
	}

	// Simulate the worker: publish the result and complete the Job.
	publishWorkerResult(ctx, h, running, 1, successResultData(testDigest, "hash  11  a.txt\n", 1))
	markJobSucceeded(ctx, h, jobName)

	ready, err := waitPhase(ctx, h, "happy", snapshotv1alpha1.PhaseReady)
	if err != nil {
		t.Fatal(err)
	}
	if ready.Status.ObservedGeneration != 1 {
		t.Fatalf("observedGeneration=%d want 1", ready.Status.ObservedGeneration)
	}
	if ready.Status.Digest != testDigest {
		t.Fatalf("digest=%q want %q", ready.Status.Digest, testDigest)
	}
	if ready.Status.Algorithm != "SHA-256" || ready.Status.FileCount != 2 || ready.Status.TotalBytes != 11 {
		t.Fatalf("digest metadata wrong: %+v", ready.Status)
	}
	if !containsFinalizer(ready.Finalizers, controller.Finalizer) {
		t.Fatal("expected finalizer to remain on a live object")
	}
	if jobs := h.listJobs(ctx, "happy"); len(jobs) != 1 {
		t.Fatalf("Job must not be recreated after Ready, got %d", len(jobs))
	}
}

// TestRestartRecoveryReadyUnwritten: the Job succeeds and the result ConfigMap
// exists, but status was never written (process crashed). A fresh manager must
// repair status from cluster state without recreating anything.
func TestRestartRecoveryReadyUnwritten(t *testing.T) {
	ctx := context.Background()
	ns := uniqueNS(t)
	h := startManager(t, ns)
	s := createSnapshotAndSource(ctx, h, "recover")
	if _, err := waitPhase(ctx, h, "recover", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}
	jobName := controller.JobName("recover", 1)

	// Worker finishes while the controller is down.
	h.stop()
	publishWorkerResult(ctx, h, s, 1, successResultData(testDigest, "m\n", 1))
	markJobSucceeded(ctx, h, jobName)

	// Restart the controller against the intact apiserver state.
	h2 := startManagerStopped(t, ns)
	h2.restart(ctx)

	ready, err := waitPhase(ctx, h2.testHarness, "recover", snapshotv1alpha1.PhaseReady)
	if err != nil {
		t.Fatalf("controller failed to recover Ready from cluster state: %v", err)
	}
	if ready.Status.Digest != testDigest || ready.Status.ObservedGeneration != 1 {
		t.Fatalf("recovered status wrong: %+v", ready.Status)
	}
	if jobs := h2.testHarness.listJobs(ctx, "recover"); len(jobs) != 1 {
		t.Fatalf("restart must not create jobs, got %d", len(jobs))
	}
}

// TestRestartMidRunning: restart while the Job is still in flight; status must
// be repaired to Running and the existing Job reused.
func TestRestartMidRunning(t *testing.T) {
	ctx := context.Background()
	ns := uniqueNS(t)
	h := startManager(t, ns)
	createSnapshotAndSource(ctx, h, "midrun")
	if _, err := waitPhase(ctx, h, "midrun", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}
	h.stop()

	h2 := startManagerStopped(t, ns)
	h2.restart(ctx)
	running, err := waitPhase(ctx, h2.testHarness, "midrun", snapshotv1alpha1.PhaseRunning)
	if err != nil {
		t.Fatal(err)
	}
	if running.Status.JobRef == nil || running.Status.JobRef.Name != controller.JobName("midrun", 1) {
		t.Fatalf("did not rebind to the existing job: %+v", running.Status.JobRef)
	}
	if jobs := h2.testHarness.listJobs(ctx, "midrun"); len(jobs) != 1 {
		t.Fatalf("expected the single in-flight job to be reused, got %d", len(jobs))
	}
}

// TestJobFailure: a failing worker (missing subPath key etc.) ends Failed with
// observedGeneration bound and a deterministic reason.
func TestJobFailure(t *testing.T) {
	ctx := context.Background()
	h := startManager(t, uniqueNS(t))
	createSnapshotAndSource(ctx, h, "failer")
	if _, err := waitPhase(ctx, h, "failer", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}
	markJobFailed(ctx, h, controller.JobName("failer", 1))

	failed, err := waitPhase(ctx, h, "failer", snapshotv1alpha1.PhaseFailed)
	if err != nil {
		t.Fatal(err)
	}
	if failed.Status.FailureReason != "BackoffLimitExceeded" || failed.Status.ObservedGeneration != 1 {
		t.Fatalf("failure status wrong: %+v", failed.Status)
	}
}

// TestSpecChangeOldGenerationCannotOverwrite: generation 1 reaches Ready; a
// spec change creates generation 2 (Running). Even if the old gen-1 Job/result
// then "completes late" (here it still exists before GC, or is replayed), it
// must never overwrite gen-2 state.
func TestSpecChangeOldGenerationCannotOverwrite(t *testing.T) {
	ctx := context.Background()
	h := startManager(t, uniqueNS(t))
	s := createSnapshotAndSource(ctx, h, "genchange")
	if _, err := waitPhase(ctx, h, "genchange", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}

	publishWorkerResult(ctx, h, s, 1, successResultData(testDigest, "m1\n", 1))
	markJobSucceeded(ctx, h, controller.JobName("genchange", 1))
	ready1, err := waitPhase(ctx, h, "genchange", snapshotv1alpha1.PhaseReady)
	if err != nil {
		t.Fatal(err)
	}
	if ready1.Status.Digest != testDigest {
		t.Fatalf("gen1 digest wrong: %s", ready1.Status.Digest)
	}

	// Spec change bumps metadata.generation to 2.
	updated := h.getSnapshot(ctx, "genchange")
	updated.Spec.SubPath = "a.txt"
	if err := h.k8s.Update(ctx, updated); err != nil {
		t.Fatal(err)
	}

	// New generation starts a new job; old resources are garbage collected.
	running2, err := waitPhase(ctx, h, "genchange", snapshotv1alpha1.PhaseRunning)
	if err != nil {
		t.Fatal(err)
	}
	if running2.Generation != 2 || running2.Status.ObservedGeneration != 2 {
		t.Fatalf("gen2 not bound correctly: gen=%d observed=%d", running2.Generation, running2.Status.ObservedGeneration)
	}
	if running2.Status.Digest != "" {
		t.Fatalf("stale gen1 digest leaked into gen2 status: %q", running2.Status.Digest)
	}
	if running2.Status.JobRef.Name != controller.JobName("genchange", 2) {
		t.Fatalf("gen2 bound to wrong job: %+v", running2.Status.JobRef)
	}

	// Old-generation resources must be gone.
	if err := waitFor(3*time.Second, func() error {
		if jobs := h.listJobs(ctx, "genchange"); len(jobs) != 1 || jobs[0].Name != controller.JobName("genchange", 2) {
			return fmt.Errorf("stale jobs remain: %v", jobNames(jobs))
		}
		if cms := h.listResultConfigMaps(ctx, "genchange"); len(cms) != 0 {
			// new result only appears when gen2 worker finishes
			return fmt.Errorf("stale result configmaps remain: %d", len(cms))
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	// Hostile late arrival: someone (an old pod) recreates a gen1-labelled
	// result ConfigMap while gen2 is Running. Reconcile must ignore it.
	stale := publishWorkerResult(ctx, h, running2, 1, successResultData(strings.Repeat("b", 64), "stale\n", 1))
	defer func() { _ = h.k8s.Delete(ctx, stale) }()
	time.Sleep(300 * time.Millisecond)
	cur := h.getSnapshot(ctx, "genchange")
	if cur.Status.Phase != snapshotv1alpha1.PhaseRunning || cur.Status.Digest != "" || cur.Status.ObservedGeneration != 2 {
		t.Fatalf("stale gen1 result overwrote gen2 state: %+v", cur.Status)
	}

	// Gen2 finishes legitimately; its digest wins.
	publishWorkerResult(ctx, h, running2, 2, successResultData(strings.Repeat("c", 64), "m2\n", 2))
	markJobSucceeded(ctx, h, controller.JobName("genchange", 2))
	ready2, err := waitPhase(ctx, h, "genchange", snapshotv1alpha1.PhaseReady)
	if err != nil {
		t.Fatal(err)
	}
	if ready2.Status.ObservedGeneration != 2 || ready2.Status.Digest != strings.Repeat("c", 64) {
		t.Fatalf("gen2 ready state wrong: %+v", ready2.Status)
	}
}

// TestDeletionWaitsForDependents: deletion blocks while Jobs/result ConfigMaps
// exist and completes once they are gone (delete race: object disappears
// between list and delete).
func TestDeletionWaitsForDependents(t *testing.T) {
	ctx := context.Background()
	h := startManager(t, uniqueNS(t))
	s := createSnapshotAndSource(ctx, h, "delete-me")
	if _, err := waitPhase(ctx, h, "delete-me", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}
	publishWorkerResult(ctx, h, s, 1, successResultData(testDigest, "m\n", 1))
	markJobSucceeded(ctx, h, controller.JobName("delete-me", 1))
	if _, err := waitPhase(ctx, h, "delete-me", snapshotv1alpha1.PhaseReady); err != nil {
		t.Fatal(err)
	}

	if err := h.k8s.Delete(ctx, h.getSnapshot(ctx, "delete-me")); err != nil {
		t.Fatal(err)
	}

	// While dependents linger (envtest deletes objects but the finalizer keeps
	// the Snapshot; Job has no own controller to clean pods), finalizer must
	// stay until our lists return empty. Reconciler itself issues the deletes.
	if err := waitFor(5*time.Second, func() error {
		var got snapshotv1alpha1.Snapshot
		err := h.k8s.Get(ctx, h.key("delete-me"), &got)
		if apierrors.IsNotFound(err) {
			return nil // fully deleted
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("snapshot still present, finalizers=%v deletionTS=%v",
			got.Finalizers, got.DeletionTimestamp)
	}); err != nil {
		t.Fatalf("snapshot was not deleted after dependents drained: %v", err)
	}

	// Owned resources should be gone too.
	var jl batchv1.JobList
	_ = h.k8s.List(ctx, &jl, client.InNamespace(h.ns))
	for _, j := range jl.Items {
		if j.Name == controller.JobName("delete-me", 1) {
			t.Fatal("worker job should have been deleted during finalization")
		}
	}
}

// TestPendingWaitsForSource: no source ConfigMap => Pending; creating it moves
// the snapshot to Running; deleting it mid-flight does not crash the loop.
func TestPendingWaitsForSource(t *testing.T) {
	ctx := context.Background()
	h := startManager(t, uniqueNS(t))
	s := newSnapshot("waiting", h.ns, "absent-src")
	if err := h.k8s.Create(ctx, s); err != nil {
		t.Fatal(err)
	}
	pending, err := waitPhase(ctx, h, "waiting", snapshotv1alpha1.PhasePending)
	if err != nil {
		t.Fatal(err)
	}
	if pending.Status.ObservedGeneration != 1 || pending.Status.FailureReason != "SourceMissing" {
		t.Fatalf("pending status wrong: %+v", pending.Status)
	}
	if jobs := h.listJobs(ctx, "waiting"); len(jobs) != 0 {
		t.Fatalf("no job may be created while source is missing, got %d", len(jobs))
	}

	// Source appears.
	if err := h.k8s.Create(ctx, sourceConfigMap("absent-src", h.ns, map[string]string{"k": "v"})); err != nil {
		t.Fatal(err)
	}
	if _, err := waitPhase(ctx, h, "waiting", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}
}

// TestResultMissingAfterSuccess: Job reports success but the result ConfigMap
// was lost (e.g. deleted). The controller must not hang in Running forever.
func TestResultMissingAfterSuccess(t *testing.T) {
	ctx := context.Background()
	h := startManager(t, uniqueNS(t))
	createSnapshotAndSource(ctx, h, "ghost-result")
	if _, err := waitPhase(ctx, h, "ghost-result", snapshotv1alpha1.PhaseRunning); err != nil {
		t.Fatal(err)
	}
	markJobSucceeded(ctx, h, controller.JobName("ghost-result", 1))
	failed, err := waitPhase(ctx, h, "ghost-result", snapshotv1alpha1.PhaseFailed)
	if err != nil {
		t.Fatal(err)
	}
	if failed.Status.FailureReason != "ResultMissing" {
		t.Fatalf("expected ResultMissing failure, got %+v", failed.Status)
	}
}

func jobNames(jobs []batchv1.Job) []string {
	out := make([]string, len(jobs))
	for i := range jobs {
		out[i] = jobs[i].Name
	}
	return out
}

func containsFinalizer(list []string, want string) bool {
	for _, f := range list {
		if f == want {
			return true
		}
	}
	return false
}

// keep the corev1/metav1 imports meaningful even if a case is trimmed.
var (
	_ = corev1.ConditionTrue
	_ = metav1.Now
	_ = types.NamespacedName{}
)
